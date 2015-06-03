#!/usr/bin/env python

import rospy
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Imu, Temperature, MagneticField
from std_msgs.msg import Float32
from crazyflie.srv import *
from std_srvs.srv import Empty, EmptyResponse

import time, sys
import math
from threading import Thread

class CrazyflieROS:
    Disconnected = 0
    Connecting = 1
    Connected = 2

    """Wrapper between ROS and Crazyflie SDK"""
    def __init__(self, link_uri, tf_prefix, roll_trim, pitch_trim, enable_logging):
        self.link_uri = link_uri
        self.tf_prefix = tf_prefix
        self.roll_trim = roll_trim
        self.pitch_trim = pitch_trim
        self.enable_logging = enable_logging
        self._cf = Crazyflie()

        self._cf.connected.add_callback(self._connected)
        self._cf.disconnected.add_callback(self._disconnected)
        self._cf.connection_failed.add_callback(self._connection_failed)
        self._cf.connection_lost.add_callback(self._connection_lost)
        self._cf.link_quality_updated.add_callback(self._link_quality_updated)

        self._cmdVel = Twist()
        self._subCmdVel = rospy.Subscriber(tf_prefix + "/cmd_vel", Twist, self._cmdVelChanged)

        self._pubImu = rospy.Publisher(tf_prefix + "/imu", Imu, queue_size=10)
        self._pubTemp = rospy.Publisher(tf_prefix + "/temperature", Temperature, queue_size=10)
        self._pubMag = rospy.Publisher(tf_prefix + "/magnetic_field", MagneticField, queue_size=10)
        self._pubPressure = rospy.Publisher(tf_prefix + "/pressure", Float32, queue_size=10)
        self._pubBattery = rospy.Publisher(tf_prefix + "/battery", Float32, queue_size=10)

        self._state = CrazyflieROS.Disconnected

        rospy.Service(tf_prefix + "/update_params", UpdateParams, self._update_params)
        rospy.Service(tf_prefix + "/emergency", Empty, self._emergency)
        self._isEmergency = False

        Thread(target=self._update).start()

    def _try_to_connect(self):
        rospy.loginfo("Connecting to %s" % self.link_uri)
        self._state = CrazyflieROS.Connecting
        self._cf.open_link(self.link_uri)

    def _connected(self, link_uri):
        """ This callback is called form the Crazyflie API when a Crazyflie
        has been connected and the TOCs have been downloaded."""

        rospy.loginfo("Connected to %s" % link_uri)
        self._state = CrazyflieROS.Connected

        if self.enable_logging:
            self._lg_imu = LogConfig(name="IMU", period_in_ms=10)
            self._lg_imu.add_variable("acc.x", "float")
            self._lg_imu.add_variable("acc.y", "float")
            self._lg_imu.add_variable("acc.z", "float")
            self._lg_imu.add_variable("gyro.x", "float")
            self._lg_imu.add_variable("gyro.y", "float")
            self._lg_imu.add_variable("gyro.z", "float")

            self._cf.log.add_config(self._lg_imu)
            if self._lg_imu.valid:
                # This callback will receive the data
                self._lg_imu.data_received_cb.add_callback(self._log_data_imu)
                # This callback will be called on errors
                self._lg_imu.error_cb.add_callback(self._log_error)
                # Start the logging
                self._lg_imu.start()
            else:
                rospy.logfatal("Could not add logconfig since some variables are not in TOC")

            self._lg_log2 = LogConfig(name="LOG2", period_in_ms=100)
            self._lg_log2.add_variable("mag.x", "float")
            self._lg_log2.add_variable("mag.y", "float")
            self._lg_log2.add_variable("mag.z", "float")
            self._lg_log2.add_variable("baro.temp", "float")
            self._lg_log2.add_variable("baro.pressure", "float")
            self._lg_log2.add_variable("pm.vbat", "float")
            # self._lg_log2.add_variable("pm.state", "uint8_t")

            self._cf.log.add_config(self._lg_log2)
            if self._lg_log2.valid:
                # This callback will receive the data
                self._lg_log2.data_received_cb.add_callback(self._log_data_log2)
                # This callback will be called on errors
                self._lg_log2.error_cb.add_callback(self._log_error)
                # Start the logging
                self._lg_log2.start()
            else:
                rospy.logfatal("Could not add logconfig since some variables are not in TOC")

        # self._lg_log3 = LogConfig(name="LOG3", period_in_ms=100)
        # self._lg_log3.add_variable("motor.m1", "int32_t")
        # self._lg_log3.add_variable("motor.m2", "int32_t")
        # self._lg_log3.add_variable("motor.m3", "int32_t")
        # self._lg_log3.add_variable("motor.m4", "int32_t")
        # self._lg_log3.add_variable("stabalizer.pitch", "float")
        # self._lg_log3.add_variable("stabalizer.roll", "float")
        # self._lg_log3.add_variable("stabalizer.thrust", "uint16_")
        # self._lg_log3.add_variable("stabalizer.yaw", "float")
        #
        # self._cf.log.add_config(self._lg_log3)
        #if self._lg_log3.valid:
        #    # This callback will receive the data
        #    self._lg_log3.data_received_cb.add_callback(self._log_data_log3)
        #    # This callback will be called on errors
        #    self._lg_log3.error_cb.add_callback(self._log_error)
        #    # Start the logging
        #    self._lg_log3.start()
        #else:
        #    rospy.logfatal("Could not add logconfig since some variables are not in TOC")


        p_toc = self._cf.param.toc.toc
        for group in p_toc.keys():
            self._cf.param.add_update_callback(group=group, name=None, cb=self._param_callback)
            for name in p_toc[group].keys():
                ros_param = "/{}/{}/{}".format(self.tf_prefix, group, name)
                cf_param = "{}.{}".format(group, name)
                if rospy.has_param(ros_param):
                    self._cf.param.set_value(cf_param, rospy.get_param(ros_param))
                else:
                    self._cf.param.request_param_update(cf_param)


    def _connection_failed(self, link_uri, msg):
        """Callback when connection initial connection fails (i.e no Crazyflie
        at the speficied address)"""
        rospy.logfatal("Connection to %s failed: %s" % (link_uri, msg))
        self._state = CrazyflieROS.Disconnected

    def _connection_lost(self, link_uri, msg):
        """Callback when disconnected after a connection has been made (i.e
        Crazyflie moves out of range)"""
        rospy.logfatal("Connection to %s lost: %s" % (link_uri, msg))
        self._state = CrazyflieROS.Disconnected

    def _disconnected(self, link_uri):
        """Callback when the Crazyflie is disconnected (called in all cases)"""
        rospy.logfatal("Disconnected from %s" % link_uri)
        self._state = CrazyflieROS.Disconnected

    def _link_quality_updated(self, percentage):
        """Called when the link driver updates the link quality measurement"""
        ##if percentage < 80:
            ##rospy.logwarn("Connection quality is: %f" % (percentage))

    def _log_error(self, logconf, msg):
        """Callback from the log API when an error occurs"""
        rospy.logfatal("Error when logging %s: %s" % (logconf.name, msg))


    def _log_data_imu(self, timestamp, data, logconf):
        """Callback froma the log API when data arrives"""
        msg = Imu()
        # ToDo: it would be better to convert from timestamp to rospy time
        msg.header.stamp = rospy.Time.now()
        msg.header.frame_id = self.tf_prefix + "/base_link"
        msg.orientation_covariance[0] = -1 # orientation not supported

        # measured in deg/s; need to convert to rad/s
        msg.angular_velocity.x = math.radians(data["gyro.x"])
        msg.angular_velocity.y = math.radians(data["gyro.y"])
        msg.angular_velocity.z = math.radians(data["gyro.z"])

        # measured in mG; need to convert to m/s^2
        msg.linear_acceleration.x = data["acc.x"] * 9.81
        msg.linear_acceleration.y = data["acc.y"] * 9.81
        msg.linear_acceleration.z = data["acc.z"] * 9.81

        self._pubImu.publish(msg)

        #print "[%d][%s]: %s" % (timestamp, logconf.name, data)

    def _log_data_log2(self, timestamp, data, logconf):
        """Callback froma the log API when data arrives"""
        msg = Temperature()
        # ToDo: it would be better to convert from timestamp to rospy time
        msg.header.stamp = rospy.Time.now()
        msg.header.frame_id = self.tf_prefix + "/base_link"
        # measured in degC
        msg.temperature = data["baro.temp"]
        self._pubTemp.publish(msg)

        # ToDo: it would be better to convert from timestamp to rospy time
        msg = MagneticField()
        msg.header.stamp = rospy.Time.now()
        msg.header.frame_id = self.tf_prefix + "/base_link"

        # measured in Tesla
        msg.magnetic_field.x = data["mag.x"]
        msg.magnetic_field.y = data["mag.y"]
        msg.magnetic_field.z = data["mag.z"]

        self._pubMag.publish(msg)

        msg = Float32()
        # hPa (=mbar)
        msg.data = data["baro.pressure"]
        self._pubPressure.publish(msg)

        # V
        msg.data = data["pm.vbat"]
        self._pubBattery.publish(msg)

    def _param_callback(self, name, value):
        ros_param = "{}/{}".format(self.tf_prefix, name.replace(".", "/"))
        rospy.set_param(ros_param, value)

    def _update_params(self, req):
        rospy.loginfo("Update parameters %s" % (str(req.params)))
        for param in req.params:
            ros_param = "/{}/{}".format(self.tf_prefix, param)
            cf_param = param.replace("/", ".")
            print(cf_param)
            #if rospy.has_param(ros_param):
            self._cf.param.set_value(cf_param, str(rospy.get_param(ros_param)))
        return UpdateParamsResponse()

    def _emergency(self, req):
        rospy.logfatal("Emergency requested!")
        self._isEmergency = True
        return EmptyResponse()

    def _send_setpoint(self):
        roll = self._cmdVel.linear.y + self.roll_trim
        pitch = self._cmdVel.linear.x + self.pitch_trim
        yawrate = self._cmdVel.angular.z
        thrust = min(max(0, int(self._cmdVel.linear.z)), 60000)
        #print(roll, pitch, yawrate, thrust)
        self._cf.commander.send_setpoint(roll, pitch, yawrate, thrust)

    def _cmdVelChanged(self, data):
        self._cmdVel = data
        if not self._isEmergency:
            self._send_setpoint()

    def _update(self):
        while not rospy.is_shutdown():
            if self._isEmergency:
                break

            if self._state == CrazyflieROS.Disconnected:
                self._try_to_connect()
            elif self._state == CrazyflieROS.Connected:
                # Crazyflie will shut down if we don't send any command for 500ms
                # Hence, make sure that we don't wait too long
                # However, if there is no connection anymore, we try to get the flie down
                if self._subCmdVel.get_num_connections() > 0:
                    self._send_setpoint()
                else:
                    self._cmdVel = Twist()
                    self._send_setpoint()
                rospy.sleep(0.2)
            else:
                rospy.sleep(0.5)
        for i in range(0, 100):
            self._cf.commander.send_setpoint(0, 0, 0, 0)
            rospy.sleep(0.01)
        # Make sure that the last packet leaves before the link is closed
        # since the message queue is not flushed before closing
        rospy.sleep(0.1)
        self._cf.close_link()

def add_crazyflie(req):
    rospy.loginfo("Adding %s as %s with trim(%f, %f). Logging: %s" % (req.uri, req.tf_prefix, req.roll_trim, req.pitch_trim, str(req.enable_logging)))
    CrazyflieROS(req.uri, req.tf_prefix, req.roll_trim, req.pitch_trim, req.enable_logging)
    return AddCrazyflieResponse()

if __name__ == '__main__':
    rospy.init_node('crazyflie_server')
    crazyflieSDK = rospy.get_param("~crazyflieSDK")

    sys.path.append(crazyflieSDK)
    import cflib
    from cflib.crazyflie import Crazyflie
    from cfclient.utils.logconfigreader import LogConfig

    # Initialize the low-level drivers (don't list the debug drivers)
    cflib.crtp.init_drivers(enable_debug_driver=False)

    rospy.Service("add_crazyflie", AddCrazyflie, add_crazyflie)
    rospy.spin()
