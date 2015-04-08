import cv2

import numpy as np
import numpy.matlib as npm
from scipy import linalg

from bot_utils.db_utils import AttrDict
from bot_geometry.rigid_transform import Quaternion, RigidTransform

kinect_v1_params = AttrDict(
    K_depth = np.array([[576.09757860, 0, 319.5],
                        [0, 576.09757860, 239.5],
                        [0, 0, 1]], dtype=np.float64), 
    K_rgb = np.array([[528.49404721, 0, 319.5],
                      [0, 528.49404721, 239.5],
                      [0, 0, 1]], dtype=np.float64), 
    H = 480, W = 640, 
    shift_offset = 1079.4753, 
    projector_depth_baseline = 0.07214
)

def construct_K(fx=500.0, fy=500.0, cx=319.5, cy=239.5): 
    """
    Create camera intrinsics from focal lengths and focal centers
    """
    K = npm.eye(3)
    K[0,0], K[1,1] = fx, fy
    K[0,2], K[1,2] = cx, cy
    return K

class CameraIntrinsic(object): 
    def __init__(self, K, D=np.zeros(4, dtype=np.float64), shape=None): 
        """
        Default init
        """
        self.K = npm.mat(K)                # Calibration matrix.
        self.D = D                         # Distortion
        self.cx, self.cy = K[0,2], K[1,2]  # Camera center.
        self.fx, self.fy = K[0,0], K[1,1]  # Focal length
        self.shape = shape                 # Image size (H,W,C): (480,640,3)

    @classmethod
    def simluate(cls): 
        """
        Simulate a 640x480 camera with 500 focal length
        """
        return cls.from_calib_params(500., 500., 320., 240.)

    @classmethod
    def from_calib_params(cls, fx, fy, cx, cy): 
        return cls(construct_K(fx, fy, cx, cy))

    @property
    def fov(self): 
        """
        Returns the field of view for each axis
        """
        return np.array([np.arctan(self.cx / self.fx), np.arctan(self.cy / self.fy)]) * 2


class CameraExtrinsic(RigidTransform): 
    def __init__(self, R=npm.eye(3), t=npm.zeros(3)):
        """
        Default init
        """
        p = RigidTransform.from_Rt(R, t)
        RigidTransform.__init__(self, xyzw=p.quat.to_xyzw(), tvec=p.tvec)

    @property
    def R(self): 
        return self.quat.to_homogeneous_matrix()[:3,:3]

    @property
    def t(self): 
        return self.tvec

    @classmethod
    def identity(cls): 
        """
        Simulate a camera at identity
        """
        return cls()

    @classmethod
    def simulate(cls): 
        """
        Simulate a camera at identity
        """
        return cls.identity()

class Camera(CameraIntrinsic, CameraExtrinsic): 
    def __init__(self, K, R, t, D=np.zeros(4, dtype=np.float64), shape=None): 
        CameraIntrinsic.__init__(self, K, D, shape=shape)
        CameraExtrinsic.__init__(self, R, t)

    @property
    def P(self): 
        Rt = self.to_homogeneous_matrix()[:3]
        return self.K * Rt     # Projection matrix

    @classmethod
    def simulate(cls): 
        """
        Simulate camera intrinsics and extrinsics
        """
        return cls.from_intrinsics_extrinsics(CameraIntrinsic.simluate(), CameraExtrinsic.simulate())

    @classmethod
    def from_intrinsics_extrinsics(cls, intrinsic, extrinsic): 
        return cls(intrinsic.K, extrinsic.R, extrinsic.t, D=intrinsic.D, shape=intrinsic.shape)

    def project(self, X):
        """
        Project [Nx3] points onto 2-D image plane [Nx2]
        """
        R, t = self.to_Rt()
	rvec,_ = cv2.Rodrigues(R)
	proj,_ = cv2.projectPoints(X, rvec, t, self.K, self.D)
	return proj.reshape((-1,2))

    def factor(self): 
        """
        Factor camera matrix P into K, R, t such that P = K[R|t].
        """
        return cv2.decomposeProjectionMatrix(self.P)

    def center(self): 
        """
        Returns the camera center, the point in space projected to (0, 0) on
        screen.
        """
        if self.cx is None: 
            raise AssertionError('cx, cy is not set')
        return npm.matrix([self.cx, self.cy])

    def set_pose(self, pose): 
        """
        Provide extrinsics to the camera
        """
        self.quat = pose.quat
        self.tvec = pose.tvec

def KinectCamera(R=npm.eye(3), t=npm.zeros(3)): 
    return Camera(kinect_v1_params.K_depth, R, t)

class DepthCamera(CameraIntrinsic): 
    def __init__(self, K, shape=(480,640), skip=1, D=np.zeros(4, dtype=np.float64)):
        CameraIntrinsic.__init__(self, K, D)

        # Retain image shape
        self.shape = shape
        self.skip = skip

        # Construct mesh for quick reconstruction
        self._build_mesh(shape=shape)
        
    def _build_mesh(self, shape): 
        H, W = shape
        xs,ys = np.arange(0,W), np.arange(0,H);
        fx_inv = 1.0 / self.fx;

        self.xs = (xs-self.cx) * fx_inv
        self.xs = self.xs[::self.skip] # skip pixels
        self.ys = (ys-self.cy) * fx_inv
        self.ys = self.ys[::self.skip] # skip pixels

        self.xs, self.ys = np.meshgrid(self.xs, self.ys);

    def reconstruct(self, depth): 
        assert(depth.shape == self.xs.shape)
        return np.dstack([self.xs * depth, self.ys * depth, depth])

def KinectDepthCamera(K=kinect_v1_params.K_depth, shape=(480,640)): 
    return DepthCamera(K=K, shape=shape)

def compute_fundamental(x1, x2, method=cv2.FM_RANSAC): 
    """
    Computes the fundamental matrix from corresponding points x1, x2 using
    the 8 point algorithm.

    Options: 
    CV_FM_7POINT for a 7-point algorithm.  N = 7
    CV_FM_8POINT for an 8-point algorithm.  N >= 8
    CV_FM_RANSAC for the RANSAC algorithm.  N >= 8
    CV_FM_LMEDS for the LMedS algorithm.  N >= 8"
    """
    assert(x1.shape == x2.shape)
    F, mask = cv2.findFundamentalMat(x1, x2, method)
    return F, mask

def compute_epipole(F):
    """ Computes the (right) epipole from a 
        fundamental matrix F. 
        (Use with F.T for left epipole.) """
    
    # return null space of F (Fx=0)
    U,S,V = linalg.svd(F)
    e = V[-1]
    return e/e[2]

def compute_essential(F, K): 
    """ Compute the Essential matrix, and R1, R2 """
    return K.T * npm.mat(F) * K

def check_visibility(camera, pts): 
    """
    Check if points are visible given fov of camera
    camera: type Camera
    """
    # Hack: only check max of the fovs
    fov = np.max(camera.fov)
    lookat = camera.R[:,2]

    v = pts - camera.tvec
    thetas = np.arccos(np.sum( np.multiply(
        np.tile(lookat, (len(pts), 1)), 
        v / np.linalg.norm(v, axis=1).reshape(-1, 1)), axis=1))

    # Provides inds mask for all points that are within fov
    return thetas < fov

def get_object_bbox(camera, pts, subsample=10, scale=1.0, min_height=10, min_width=10, visualize=False): 
    pts2d = camera.project(pts[::subsample].astype(np.float32))

    # Min-max bounds
    x0, x1 = int(max(0, np.min(pts2d[:,0]))), int(min(camera.shape[1]-1, np.max(pts2d[:,0])))
    y0, y1 = int(max(0, np.min(pts2d[:,1]))), int(min(camera.shape[0]-1, np.max(pts2d[:,1])))

    # Only return points within-image bounds
    valid = np.bitwise_and(np.bitwise_and(pts2d[:,0] >= 0, pts2d[:,0] < camera.shape[1]), \
                           np.bitwise_and(pts2d[:,1] >= 0, pts2d[:,1] < camera.shape[0]))
    pts2d = pts2d[valid]

    # Check median center 
    xmed, ymed = np.median(pts2d[:,0]), np.median(pts2d[:,1])
    if (xmed >= 0 and ymed >= 0 and xmed <= camera.shape[1] and ymed < camera.shape[0]) and \
       (y1-y0) >= min_height and (x1-x0) >= min_width: 

        # Median depth of the candidate object
        # Transform points in camera frame, and check z-vector: 
        # [p_c = T_cw * p_w]
        depth = np.median((camera * pts[::subsample])[:,2])
        if depth < 0: return [None] * 3
        # assert(depth >= 0), "Depth is less than zero, add check for this."

        # if visualize: 
        #     draw_utils.publish_cloud('obj_cloud', pts[::subsample], c='r', frame_id='KINECT')
        #     draw_utils.publish_point_type('obj_distance', 
        #                                   np.vstack([camera.inverse().tvec.reshape(-1,3), 
        #                                              pts[0].reshape(-1,3)]), 
        #                                   c='r', point_type='LINES', frame_id='KINECT')

        if scale != 1.0: 
            w2, h2 = (scale-1.0) * (x1-x0) / 2, (scale-1.0) * (y1-y0) / 2
            x0, x1 = int(max(0, x0 - w2)), int(min(x1 + w2, camera.shape[1]-1))
            y0, y1 = int(max(0, y0 - h2)), int(min(y1 + h2, camera.shape[0]-1))
        return pts2d.astype(np.int32), {'left':x0, 'right':x1, 'top':y0, 'bottom':y1}, depth
    else: 
        return [None] * 3

# def plot_epipolar_line(im, F, x, epipole=None, show_epipole=True):
#   """
#   Plot the epipole and epipolar line F * x = 0.
#   """
#   import pylab

#   m, n = im.shape[:2]
#   line = numpy.dot(F, x)

#   t = numpy.linspace(0, n, 100)
#   lt = numpy.array([(line[2] + line[0] * tt) / (-line[1]) for tt in t])

#   ndx = (lt >= 0) & (lt < m)
#   pylab.plot(t[ndx], lt[ndx], linewidth=2)

#   if show_epipole:
#     if epipole is None:
#       epipole = compute_right_epipole(F)
#     pylab.plot(epipole[0] / epipole[2], epipole[1] / epipole[2], 'r*')
