#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight
from styx_msgs.msg import Lane
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
import tf
import cv2
import yaml

from scipy.spatial import KDTree
import math
import numpy as np

from darknet_ros_msgs.msg import BoundingBox
from darknet_ros_msgs.msg import BoundingBoxes

STATE_COUNT_THRESHOLD = 3

class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = None
        # Waypoint KD tree
        self.waypoints_2d = None
        self.waypoint_tree = None
        
        self.camera_image = None
        self.lights = []
        
        sub1 = rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        sub2 = rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        sub3 = rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        sub4 = rospy.Subscriber('/image_color', Image, self.image_cb)
        
         # darknet_ros message
        sub5 = rospy.Subscriber('/darknet_ros/bounding_boxes', BoundingBoxes, self.detected_bb_cb)
        
        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)
        
        # Get simulator_mode parameter (1== ON, 0==OFF)
        self.simulator_mode = rospy.get_param("/simulator_mode")
        
        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)
        
        if int(self.simulator_mode) == 0:
            self.cropped_tl_bb_pub = rospy.Publisher('/cropped_bb', Image, queue_size=1)
            
        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0
        
        rospy.spin()

    def pose_cb(self, msg):
        self.pose = msg
        
    def waypoints_cb(self, waypoints):
        self.waypoints = waypoints
        if not self.waypoints_2d:
            self.waypoints_2d = [[waypoint.pose.pose.position.x, waypoint.pose.pose.position.y] for waypoint in waypoints.waypoints]
            self.waypoint_tree = KDTree(self.waypoints_2d)

    def traffic_cb(self, msg):
        self.lights = msg.lights

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light's stop line to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def detected_bb_cb(self, mgs):
        self.TL_BB_list = []
        simulator_bb_size_threshold = 85
        site_bb_size_threshold = 40
        simulator_bb_probability = 0.85
        site_bb_probability = 0.25
        
        if int(self.simulator_mode) == 1:
            prob_thresh = simulator_bb_probability
            size_thresh = simulator_bb_size_threshold
        else:
            prob_thresh = site_bb_probability
            size_thresh = site_bb_size_threshold
        
        for bb in msg.bounding_boxes:
            # Simulator mode: Bounding Box class should be 'traffic light' with probability >= 85%
            # Site Mode: Bounding Box class should be 'traffic light' with probability >= 25%
            if str(bb.Class) == 'traffic light' and bb.probability >= prob_thresh:
                # Simulator mode: If diagonal size of bounding box is more than 85px
                # Site mode: If diagonal size of bounding box is more than 80px
                if math.sqrt((bb.xmin - bb.xmax)**2 + (bb.ymin - bb.ymax)**2) >= size_thresh:
                    self.TL_BB_list.append(bb)

                    # if running in site mode/ROS bag mode
                    if int(self.simulator_mode) == 0:
                        '''The ROS bag version only has video data. Hence no waypoints are loaded and get light function is not called.
                            So to check detection in ROS bag video, we do TL state classification here itself.
                        '''
                        # Get the camera image
                        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")
                        # Crop image
                        bb_image = cv_image[bb.ymin:bb.ymax, bb.xmin:bb.xmax]
                        self.light_classifier.detect_light_state(bb_image)
                        
    def get_closest_waypoint(self, x, y):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """
        #TODO implement
        closet_idx = self.waypoints.query([x,y],1)[1]
        return closet_idx
    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        if(not self.has_image):
            self.prev_light_loc = None
            return False

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")
        #Get classification
        return self.light_classifier.get_classification(cv_image, self.TL_BB_list, self.simulator_mode)

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closes to the upcoming stop line for a traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        closest_light = None
        line_wp_idx = None
        state = TrafficLight.UNKNOWN
        #TODO find the closest visible traffic light (if one exists)
        
        # List of positions that correspond to the line to stop in front of for a given intersection
        stop_line_positions = self.config['stop_line_positions']
        if(self.pose):
            #car_position = self.get_closest_waypoint(self.pose.pose)
            car_wp_idx = self.get_closest_waypoint(self.pose.pose.position.x, self.pose.pose.position.y)
            diff = len(self.waypoints.waypoints)
            
            for i, light in enumerate(self.lights):
                # Get stop line waypoint index
                line = stop_line_positions[i]
                tem_wp_idx = self.get_closest_waypoint(line[0], line[1])
                # find closet stop line waypoint index
                d = tem_wp_idx - car_wp_idx
                if 0 <=d < diff:
                    diff = d
                    closest_light = light
                    line_wp_idx = tem_wp_idx                                       
        
        if closest_light:
            state = self.get_light_state(closest_light)
            return line_wp_idx, state
        
        return -1, TrafficLight.UNKNOWN

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')
