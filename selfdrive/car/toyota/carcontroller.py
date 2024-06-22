from cereal import car
from openpilot.common.numpy_fast import clip, interp
from openpilot.common.params import Params
from openpilot.selfdrive.car import apply_meas_steer_torque_limits, apply_std_steer_angle_limits, common_fault_avoidance, \
                          create_gas_interceptor_command, make_can_msg
from openpilot.selfdrive.car.interfaces import CarControllerBase
from openpilot.selfdrive.car.toyota import toyotacan
from openpilot.selfdrive.car.toyota.values import CAR, STATIC_DSU_MSGS, NO_STOP_TIMER_CAR, TSS2_CAR, RADAR_ACC_CAR, \
                                        MIN_ACC_SPEED, PEDAL_TRANSITION, CarControllerParams, ToyotaFlags, \
                                        UNSUPPORTED_DSU_CAR
from opendbc.can.packer import CANPacker
from common.conversions import Conversions as CV

SteerControlType = car.CarParams.SteerControlType
VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState

# LKA limits
# EPS faults if you apply torque while the steering rate is above 100 deg/s for too long
MAX_STEER_RATE = 100  # deg/s
MAX_STEER_RATE_FRAMES = 18  # tx control frames needed before torque can be cut

# EPS allows user torque above threshold for 50 frames before permanently faulting
MAX_USER_TORQUE = 500

# LTA limits
# EPS ignores commands above this angle and causes PCS to fault
MAX_LTA_ANGLE = 94.9461  # deg
MAX_LTA_DRIVER_TORQUE_ALLOWANCE = 150  # slightly above steering pressed allows some resistance when changing lanes

# PCM compensatory force calculation threshold
# a variation in accel command is more pronounced at higher speeds, let compensatory forces ramp to zero before
# applying when speed is high
COMPENSATORY_CALCULATION_THRESHOLD_V = [-0.2, 0.]  # m/s^2
COMPENSATORY_CALCULATION_THRESHOLD_BP = [0., 23.]  # m/s


#BSM (@arne182, @rav4kumar)
LEFT_BLINDSPOT = b'\x41'
RIGHT_BLINDSPOT = b'\x42'

def set_blindspot_debug_mode(lr,enable):
  if enable:
    m = lr + b'\x02\x10\x60\x00\x00\x00\x00'
  else:
    m = lr + b'\x02\x10\x01\x00\x00\x00\x00'
  return make_can_msg(0x750, m, 0)

def poll_blindspot_status(lr):
  m = lr + b'\x02\x21\x69\x00\x00\x00\x00'
  return make_can_msg(0x750, m, 0)

#AUTO LOCK/UNLOCK (@dragonpilot-community)
GearShifter = car.CarState.GearShifter
UNLOCK_CMD = b'\x40\x05\x30\x11\x00\x40\x00\x00'
LOCK_CMD = b'\x40\x05\x30\x11\x00\x80\x00\x00'
LOCK_AT_SPEED = 10 * CV.KPH_TO_MS


class CarController(CarControllerBase):
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.params = CarControllerParams(self.CP)
    self.frame = 0
    self.last_steer = 0
    self.last_angle = 0
    self.alert_active = False
    self.last_standstill = False
    self.standstill_req = False
    self.steer_rate_counter = 0
    self.distance_button = 0
    self.prohibit_neg_calculation = True

    self.packer = CANPacker(dbc_name)
    self.gas = 0
    self.accel = 0

    self.param_s = Params()
    self._reverse_acc_change = self.param_s.get_bool("ReverseAccChange")
    self._sng_hack = self.param_s.get_bool("ToyotaSnG")

    self._toyota_auto_lock = self.param_s.get_bool("ToyotaAutoLock")
    self._toyota_auto_unlock = self.param_s.get_bool("ToyotaAutoUnlock")
    self._toyota_auto_lock_gear_prev = GearShifter.park
    self._toyota_auto_lock_once = False

    # sp - bsm
    self.sp_toyota_enhanced_bsm = self.param_s.get_bool("sp_toyota_enhanced_bsm")
    self._blindspot_debug_enabled_left = False
    self._blindspot_debug_enabled_right = False
    self._blindspot_frame = 0
    self._blindspot_rate = 20
    self._blindspot_always_on = True

    # sp - auto break hold (https://github.com/AlexandreSato)
    self.sp_toyota_auto_hold = self.param_s.get_bool("sp_toyota_auto_hold") and self.CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR)

  def update(self, CC, CS, now_nanos):
    actuators = CC.actuators
    hud_control = CC.hudControl
    pcm_cancel_cmd = CC.cruiseControl.cancel
    lat_active = CC.latActive and abs(CS.out.steeringTorque) < MAX_USER_TORQUE and abs(CS.out.steeringAngleDeg) < MAX_USER_TORQUE
    stopping = actuators.longControlState == LongCtrlState.stopping

    # *** control msgs ***
    can_sends = []

    # sp - door auto lock / unlock logic (@dragonpilot-community)
    # thanks to AlexandreSato & cydia2020
    # https://github.com/AlexandreSato/animalpilot/blob/personal/doors.py
    if not CS.out.doorOpen:
      gear = CS.out.gearShifter
      if gear == GearShifter.park and self._toyota_auto_lock_gear_prev != gear:
        if self._toyota_auto_unlock:
          can_sends.append(make_can_msg(0x750, UNLOCK_CMD, 0))
        self._toyota_auto_lock_once = False
      elif gear == GearShifter.drive and not self._toyota_auto_lock_once and CS.out.vEgo >= LOCK_AT_SPEED:
        if self._toyota_auto_lock:
          can_sends.append(make_can_msg(0x750, LOCK_CMD, 0))
        self._toyota_auto_lock_once = True
      self._toyota_auto_lock_gear_prev = gear

    # Enable blindspot debug mode once (@arne182, @rav4kumar)
    # let's keep all the commented out code for easy debug purpose for future.
    if self.sp_toyota_enhanced_bsm:
      if self.frame > 200:
        #left bsm
        if not self._blindspot_debug_enabled_left:
          if (self._blindspot_always_on or CS.out.vEgo > 6): # eagle eye camera will stop working if right bsm is switched on under 6m/s
            can_sends.append(set_blindspot_debug_mode(LEFT_BLINDSPOT, True))
            self._blindspot_debug_enabled_left = True
            # print("bsm debug left, on")
        else:
          if not self._blindspot_always_on and self.frame - self._blindspot_frame > 50:
            can_sends.append(set_blindspot_debug_mode(LEFT_BLINDSPOT, False))
            self._blindspot_debug_enabled_left = False
            # print("bsm debug left, off")
          if self.frame % self._blindspot_rate == 0:
            can_sends.append(poll_blindspot_status(LEFT_BLINDSPOT))
            # if CS.out.leftBlinker:
            self._blindspot_frame = self.frame
            # print(self._blindspot_frame)
            # print("bsm poll left")
        #right bsm
        if not self._blindspot_debug_enabled_right:
          if (self._blindspot_always_on or CS.out.vEgo > 6): # eagle eye camera will stop working if right bsm is switched on under 6m/s
            can_sends.append(set_blindspot_debug_mode(RIGHT_BLINDSPOT, True))
            self._blindspot_debug_enabled_right = True
            # print("bsm debug right, on")
        else:
          if not self._blindspot_always_on and self.frame - self._blindspot_frame > 50:
            can_sends.append(set_blindspot_debug_mode(RIGHT_BLINDSPOT, False))
            self._blindspot_debug_enabled_right = False
            # print("bsm debug right, off")
          if self.frame % self._blindspot_rate == self._blindspot_rate / 2:
            can_sends.append(poll_blindspot_status(RIGHT_BLINDSPOT))
            # if CS.out.rightBlinker:
            self._blindspot_frame = self.frame
            # print(self._blindspot_frame)
            # print("bsm poll right")

    # *** steer torque ***
    new_steer = int(round(actuators.steer * self.params.STEER_MAX))
    apply_steer = apply_meas_steer_torque_limits(new_steer, self.last_steer, CS.out.steeringTorqueEps, self.params)

    # >100 degree/sec steering fault prevention
    self.steer_rate_counter, apply_steer_req = common_fault_avoidance(abs(CS.out.steeringRateDeg) >= MAX_STEER_RATE, lat_active,
                                                                      self.steer_rate_counter, MAX_STEER_RATE_FRAMES)

    if not lat_active:
      apply_steer = 0

    # *** steer angle ***
    if self.CP.steerControlType == SteerControlType.angle:
      # If using LTA control, disable LKA and set steering angle command
      apply_steer = 0
      apply_steer_req = False
      if self.frame % 2 == 0:
        # EPS uses the torque sensor angle to control with, offset to compensate
        apply_angle = actuators.steeringAngleDeg + CS.out.steeringAngleOffsetDeg

        # Angular rate limit based on speed
        apply_angle = apply_std_steer_angle_limits(apply_angle, self.last_angle, CS.out.vEgoRaw, self.params)

        if not lat_active:
          apply_angle = CS.out.steeringAngleDeg + CS.out.steeringAngleOffsetDeg

        self.last_angle = clip(apply_angle, -MAX_LTA_ANGLE, MAX_LTA_ANGLE)

    self.last_steer = apply_steer

    # toyota can trace shows STEERING_LKA at 42Hz, with counter adding alternatively 1 and 2;
    # sending it at 100Hz seem to allow a higher rate limit, as the rate limit seems imposed
    # on consecutive messages
    can_sends.append(toyotacan.create_steer_command(self.packer, apply_steer, apply_steer_req and lat_active))

    # STEERING_LTA does not seem to allow more rate by sending faster, and may wind up easier
    if self.frame % 2 == 0 and self.CP.carFingerprint in TSS2_CAR:
      lta_active = lat_active and self.CP.steerControlType == SteerControlType.angle
      # cut steering torque with TORQUE_WIND_DOWN when either EPS torque or driver torque is above
      # the threshold, to limit max lateral acceleration and for driver torque blending respectively.
      full_torque_condition = (abs(CS.out.steeringTorqueEps) < self.params.STEER_MAX and
                               abs(CS.out.steeringTorque) < MAX_LTA_DRIVER_TORQUE_ALLOWANCE)

      # TORQUE_WIND_DOWN at 0 ramps down torque at roughly the max down rate of 1500 units/sec
      torque_wind_down = 100 if lta_active and full_torque_condition else 0
      can_sends.append(toyotacan.create_lta_steer_command(self.packer, self.CP.steerControlType, self.last_angle,
                                                          lta_active, self.frame // 2, torque_wind_down))

    # *** gas and brake ***
    if self.CP.enableGasInterceptorDEPRECATED and CC.longActive:
      MAX_INTERCEPTOR_GAS = 0.5
      # RAV4 has very sensitive gas pedal
      if self.CP.carFingerprint in (CAR.TOYOTA_RAV4, CAR.TOYOTA_RAV4H, CAR.TOYOTA_HIGHLANDER):
        PEDAL_SCALE = interp(CS.out.vEgo, [0.0, MIN_ACC_SPEED, MIN_ACC_SPEED + PEDAL_TRANSITION], [0.15, 0.3, 0.0])
      elif self.CP.carFingerprint in (CAR.TOYOTA_COROLLA,):
        PEDAL_SCALE = interp(CS.out.vEgo, [0.0, MIN_ACC_SPEED, MIN_ACC_SPEED + PEDAL_TRANSITION], [0.3, 0.4, 0.0])
      else:
        PEDAL_SCALE = interp(CS.out.vEgo, [0.0, MIN_ACC_SPEED, MIN_ACC_SPEED + PEDAL_TRANSITION], [0.4, 0.5, 0.0])
      # offset for creep and windbrake
      pedal_offset = interp(CS.out.vEgo, [0.0, 2.3, MIN_ACC_SPEED + PEDAL_TRANSITION], [-.4, 0.0, 0.2])
      pedal_command = PEDAL_SCALE * (actuators.accel + pedal_offset)
      interceptor_gas_cmd = clip(pedal_command, 0., MAX_INTERCEPTOR_GAS)
    else:
      interceptor_gas_cmd = 0.
    pcm_accel_cmd = clip(actuators.accel, self.params.ACCEL_MIN, self.params.ACCEL_MAX)

    # TODO: probably can delete this. CS.pcm_acc_status uses a different signal
    # than CS.cruiseState.enabled. confirm they're not meaningfully different
    if not (CC.enabled and CS.out.cruiseState.enabled) and CS.pcm_acc_status:
      pcm_cancel_cmd = 1

    if self.CP.pcmCruise and not CS.pcm_acc_status and CS.out.cruiseState.enabled and self.CP.carFingerprint not in UNSUPPORTED_DSU_CAR:
      pcm_cancel_cmd = 1

    # on entering standstill, send standstill request
    if CS.out.standstill and not self.last_standstill and (self.CP.carFingerprint not in NO_STOP_TIMER_CAR or self.CP.enableGasInterceptorDEPRECATED) and \
      not self._sng_hack:
      self.standstill_req = True
    if CS.pcm_acc_status != 8:
      # pcm entered standstill or it's disabled
      self.standstill_req = False

    self.last_standstill = CS.out.standstill

    #sp: Automatic Brake Hold (https://github.com/AlexandreSato/)
    if self.sp_toyota_auto_hold and self.frame % 2 == 0:
      if CS._brakehold:
        can_sends.append(toyotacan.create_brakehold_command(self.packer, {}, True if self.frame % 730 < 727 else False))
      else:
        can_sends.append(toyotacan.create_brakehold_command(self.packer, CS.stock_aeb, False))

    # handle UI messages
    fcw_alert = hud_control.visualAlert == VisualAlert.fcw
    steer_alert = hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw)

    # we can spam can to cancel the system even if we are using lat only control
    if (self.frame % 3 == 0 and self.CP.openpilotLongitudinalControl) or pcm_cancel_cmd:
      lead = hud_control.leadVisible or CS.out.vEgo < 12.  # at low speed we always assume the lead is present so ACC can be engaged
      reverse_acc = 2 if self._reverse_acc_change else 1

      # Press distance button until we are at the correct bar length. Only change while enabled to avoid skipping startup popup
      if self.frame % 6 == 0 and self.CP.openpilotLongitudinalControl:
        desired_distance = 4 - hud_control.leadDistanceBars
        if CS.out.cruiseState.enabled and CS.pcm_follow_distance != desired_distance:
          self.distance_button = not self.distance_button
        else:
          self.distance_button = 0

      # Lexus IS uses a different cancellation message
      if pcm_cancel_cmd and self.CP.carFingerprint in UNSUPPORTED_DSU_CAR:
        can_sends.append(toyotacan.create_acc_cancel_command(self.packer))
      elif self.CP.openpilotLongitudinalControl:
        can_sends.append(toyotacan.create_accel_command(self.packer, pcm_accel_cmd, actuators.accel, pcm_cancel_cmd, self.standstill_req, lead, CS.acc_type, fcw_alert,
                                                        self.distance_button, reverse_acc))
        self.accel = pcm_accel_cmd
      else:
        can_sends.append(toyotacan.create_accel_command(self.packer, 0, 0, pcm_cancel_cmd, False, lead, CS.acc_type, False, self.distance_button, reverse_acc))

    if self.frame % 2 == 0 and self.CP.enableGasInterceptorDEPRECATED and self.CP.openpilotLongitudinalControl:
      # send exactly zero if gas cmd is zero. Interceptor will send the max between read value and gas cmd.
      # This prevents unexpected pedal range rescaling
      can_sends.append(create_gas_interceptor_command(self.packer, interceptor_gas_cmd, self.frame // 2))
      self.gas = interceptor_gas_cmd

    # *** hud ui ***
    if self.CP.carFingerprint != CAR.TOYOTA_PRIUS_V:
      # ui mesg is at 1Hz but we send asap if:
      # - there is something to display
      # - there is something to stop displaying
      send_ui = False
      if ((fcw_alert or steer_alert) and not self.alert_active) or \
         (not (fcw_alert or steer_alert) and self.alert_active):
        send_ui = True
        self.alert_active = not self.alert_active
      elif pcm_cancel_cmd:
        # forcing the pcm to disengage causes a bad fault sound so play a good sound instead
        send_ui = True

      if self.frame % 20 == 0 or send_ui:
        can_sends.append(toyotacan.create_ui_command(self.packer, steer_alert, pcm_cancel_cmd, hud_control.leftLaneVisible,
                                                     hud_control.rightLaneVisible, hud_control.leftLaneDepart,
                                                     hud_control.rightLaneDepart, CC.latActive and lat_active, CS.lkas_hud, CS.madsEnabled))

      if (self.frame % 100 == 0 or send_ui) and (self.CP.enableDsu or self.CP.flags & ToyotaFlags.DISABLE_RADAR.value):
        can_sends.append(toyotacan.create_fcw_command(self.packer, fcw_alert))

    # *** static msgs ***
    for addr, cars, bus, fr_step, vl in STATIC_DSU_MSGS:
      if self.frame % fr_step == 0 and self.CP.enableDsu and self.CP.carFingerprint in cars:
        can_sends.append(make_can_msg(addr, vl, bus))

    # keep radar disabled
    if self.frame % 20 == 0 and self.CP.flags & ToyotaFlags.DISABLE_RADAR.value:
      can_sends.append([0x750, 0, b"\x0F\x02\x3E\x00\x00\x00\x00\x00", 0])

    new_actuators = actuators.as_builder()
    new_actuators.steer = apply_steer / self.params.STEER_MAX
    new_actuators.steerOutputCan = apply_steer
    new_actuators.steeringAngleDeg = self.last_angle
    new_actuators.accel = self.accel
    new_actuators.gas = self.gas

    self.frame += 1
    return new_actuators, can_sends
