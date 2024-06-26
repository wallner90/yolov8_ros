# Copyright (C) 2023  Miguel Ángel González Santamarta

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import numpy as np

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile
from rclpy.qos import QoSHistoryPolicy
from rclpy.qos import QoSDurabilityPolicy
from rclpy.qos import QoSReliabilityPolicy

import message_filters
from cv_bridge import CvBridge

from ultralytics.trackers import BOTSORT, BYTETracker
from ultralytics.trackers.basetrack import BaseTrack
from ultralytics.utils import IterableSimpleNamespace, yaml_load
from ultralytics.utils.checks import check_requirements, check_yaml
from ultralytics.engine.results import Boxes
from ultralytics.trackers.utils.gmc import GMC

from sensor_msgs.msg import Image
from yolov8_msgs.msg import Detection
from yolov8_msgs.msg import DetectionArray
from yolov8_msgs.srv import SetTrackedObject


from boxmot import TRACKERS
from boxmot.tracker_zoo import create_tracker
from boxmot.utils import ROOT, WEIGHTS, TRACKER_CONFIGS
from boxmot.utils.checks import TestRequirements

import torch

from pathlib import Path
import yaml
import cv2

class TrackingNode(Node):

    def __init__(self) -> None:
        super().__init__("tracking_node")

        # params
        self.declare_parameter("tracker", "/workspace/src/yolov8_ros/yolov8_bringup/botsort.yaml")
        tracker = self.get_parameter(
            "tracker").get_parameter_value().string_value

        self.declare_parameter("image_reliability",
                               QoSReliabilityPolicy.BEST_EFFORT)
        image_qos_profile = QoSProfile(
            reliability=self.get_parameter(
                "image_reliability").get_parameter_value().integer_value,
            history=QoSHistoryPolicy.KEEP_LAST,
            durability=QoSDurabilityPolicy.VOLATILE,
            depth=1
        )

        self.gmc = GMC()
        self.cv_bridge = CvBridge()
        self.tracker = self.create_tracker(tracker)

        self.boxmot_tracker_method = 'strongsort'
        assert self.boxmot_tracker_method in TRACKERS, \
            f"'{self.boxmot_tracker_method}' is not supported. Supported ones are {TRACKERS}"
        # self.boxmot_tracker_config = TRACKER_CONFIGS / (self.boxmot_tracker_method + '.yaml')
        self.boxmot_tracker_config = Path('/workspace/src/yolov8_ros/yolov8_bringup/strongsort_boxmot.yaml')
        with open(self.boxmot_tracker_config, 'r') as f:
            config = yaml.safe_load(f)
        # self.boxmot_reid_model = WEIGHTS / ('osnet_x0_25_msmt17.pt')
        # self.boxmot_reid_model = Path('/workspace/osnet_x0_25_msmt17.pt')
        self.boxmot_reid_model = Path('/workspace/osnet_ain_x1_0_msmt17_cosine.pt')
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.boxmot_half = False
        self.boxmot_per_class = True
        self.boxmot_tracker = create_tracker(self.boxmot_tracker_method, 
                                             self.boxmot_tracker_config, 
                                             self.boxmot_reid_model, 
                                             self.device, 
                                             self.boxmot_half,
                                             self.boxmot_per_class)
        # self.boxmot_tracker.max_time_lost = 3000
        # self.boxmot_tracker.buffer_size = 3000
        # self.boxmot_tracker.track_buffer = 3000
        # self.boxmot_tracker.track_thres = 0.5
        if hasattr(self.boxmot_tracker, 'model'):
            self.boxmot_tracker.model.warmup()

        # pubs
        self._pub = self.create_publisher(DetectionArray, "tracking", 10)

        # subs
        image_sub = message_filters.Subscriber(
            self, Image, "image_raw", qos_profile=image_qos_profile)
        detections_sub = message_filters.Subscriber(
            self, DetectionArray, "detections", qos_profile=10)

        self._synchronizer = message_filters.ApproximateTimeSynchronizer(
            (image_sub, detections_sub), 10, 0.5)
        self._synchronizer.registerCallback(self.detections_cb)

        self.set_tracked_object_srv = self.create_service(
            SetTrackedObject, "set_tracked_object", self.set_tracked_object_cb)
        
        self.selected_object_id = None
        
    def set_tracked_object_cb(self, request, response):
        if request.object_id == -1:
            self.reset_tracker()
            response.success = True
            self.get_logger().info('Reset tracked object ID')
            return response
        else:
            self.selected_object_id = request.object_id
            response.success = True
            self.get_logger().info(f'Set tracked object ID to: {self.selected_object_id}')  # Add this line
            return response
    
    def reset_tracker(self):
        self.selected_object_id = None

    def create_tracker(self, tracker_yaml: str) -> BaseTrack:

        TRACKER_MAP = {"bytetrack": BYTETracker, "botsort": BOTSORT}
        check_requirements("lap")  # for linear_assignment

        tracker = check_yaml(tracker_yaml)
        cfg = IterableSimpleNamespace(**yaml_load(tracker))

        assert cfg.tracker_type in ["bytetrack", "botsort"], \
            f"Only support 'bytetrack' and 'botsort' for now, but got '{cfg.tracker_type}'"
        tracker = TRACKER_MAP[cfg.tracker_type](args=cfg, frame_rate=1)
        return tracker

    def detections_cb(self, img_msg: Image, detections_msg: DetectionArray) -> None:

        tracked_detections_msg = DetectionArray()
        tracked_detections_msg.header = img_msg.header

        # convert image
        cv_image = self.cv_bridge.imgmsg_to_cv2(img_msg)

        # parse detections
        detection_list = []
        detection: Detection
        for detection in detections_msg.detections:
            detection_list.append(
                [
                    detection.bbox.center.position.x - detection.bbox.size.x / 2,
                    detection.bbox.center.position.y - detection.bbox.size.y / 2,
                    detection.bbox.center.position.x + detection.bbox.size.x / 2,
                    detection.bbox.center.position.y + detection.bbox.size.y / 2,
                    detection.score,
                    detection.class_id
                ]
            )

        # tracking
        if len(detection_list) > 0:

            # det = Boxes(
            #     np.array(detection_list),
            #     (img_msg.height, img_msg.width)
            # )

            # tracks = self.tracker.update(det, cv_image)
            boxmot_tracks = self.boxmot_tracker.update(np.array(detection_list), cv_image)
            # self.boxmot_tracker.plot_results(cv_image, show_trajectories=True)
            # cv2.imshow('tracking', cv_image)
            # cv2.waitKey(1)

            if len(boxmot_tracks) > 0:

                for t in boxmot_tracks:

                    tracked_box = Boxes(
                        t[:-1], (img_msg.height, img_msg.width))

                    try:
                        tracked_detection: Detection = detections_msg.detections[int(
                            t[-1])]
                    except:
                        # if the tracked object is not in the detections, skip
                        continue

                    
                    # If selected_object_id is not None and does not match the current track's id, skip this track
                    if self.selected_object_id is not None and self.selected_object_id != -1 and tracked_box.id != self.selected_object_id:
                        continue

                    # get boxes values
                    box = tracked_box.xywh[0]
                    tracked_detection.bbox.center.position.x = float(box[0])
                    tracked_detection.bbox.center.position.y = float(box[1])
                    tracked_detection.bbox.size.x = float(box[2])
                    tracked_detection.bbox.size.y = float(box[3])

                    # get track id
                    track_id = ""
                    if tracked_box.is_track:
                        track_id = str(int(tracked_box.id))
                    tracked_detection.id = track_id

                    # append msg
                    tracked_detections_msg.detections.append(tracked_detection)

        # publish detections
        self._pub.publish(tracked_detections_msg)


def main():
    rclpy.init()
    node = TrackingNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()