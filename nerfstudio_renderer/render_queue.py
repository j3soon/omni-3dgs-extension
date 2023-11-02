import time
import threading
from renderer import *

class RendererCameraConfig:
	"""
	This class contains functions used to load
	camera configurations for the NerfStudioRenderQueue to use.

	The configuration is a list of dicts.
	The NerfStudioRenderQueue is then able to render differently
	sized images with respect to each configuration, 
	for performance considerations for example.
	"""

	def __init__(self, cameras_config):
		"""
		Parameters
		----------
		cameras_config : list[dict]
			A list of dicts that describes different camera configurations.
			Each element is of the form { 
                'width': int,                       # The targeted rendered width
                'height': int,                      # The targeted rendered height
                'fov': float,                       # The targeted rendered height
                'num_allowed_render_calls': int,    # The maximum number of render calls allowed for this configuration
                'delay_before_render_call': int     # The delay before making a render call for this configuration
            }
		"""
		self.cameras = cameras_config

	def default_config():
		"""
		Returns a default configuration, where there are 2 cameras,
		one for accelerated and estimated rendering, and another for 
		high-resolution display.

		Returns
		----------
		RendererCameraConfig
			A default config.
		"""
		return RendererCameraConfig([
			{ 'width': 90,  'height': 42,  'fov': 72, 'num_allowed_render_calls': 5, 'delay_before_render_call': 0   },
			{ 'width': 900, 'height': 420, 'fov': 72, 'num_allowed_render_calls': 2, 'delay_before_render_call': 0.1 }
		])

	def load_config(file_path=None):
		"""
		Returns a configuration defined by a json-formatted file.
		
		Parameters
		----------
		file_path : str, optional
			The path to the config file.

		Returns
		----------
		RendererCameraConfig
			A config specified by `file_path`, or a default one.
		"""
		if file_path is None:
			return RendererCameraConfig.default_config()
		with open(file_path, 'r') as f:
			return RendererCameraConfig(json.load(f))
		
	def __len__(self):
		"""
		Returns
		----------
		int
			The number of cameras in this configuration list.
		"""
		return len(self.cameras)
	
	def __getitem__(self, idx):
		"""
		Returns
		----------
		dict
			The camera configuration indexed by `idx`.
		"""
		return self.cameras[idx]

class NerfStudioRenderQueue():
    """
	The class encapsulates NerfStudioRenderer and provides
    a mechanism that aims at minimizing rendering latency,
    via an interface that allows registration of rendering
    requests. The render queue attempts to deliver 
    rendering results of the latest request in time, so 
    requests are not guaranteed to be served.

    Attributes
    ----------
	camera_config : RendererCameraConfig
        The different configurations of cameras (different qualities, etc.).

    renderer : NerfStudioRenderer
        The NerfStudioRenderer used to actually give rendered images.
	"""
    
    def __init__(self, model_config_path, camera_config_path=None, eval_num_rays_per_chunk=None):
        """
        Parameters
        ----------
        model_config_path : str
            The path to model configuration .yml file.

        camera_config_path : str, optional
            The path to the config file. 
			Uses `RendererCameraConfig.default_config()` when not assigned.

        eval_num_rays_per_chunk : int, optional
            The parameter `eval_num_rays_per_chunk` to pass to `nerfstudio.utils.eval_utils.eval_setup`
        """
        # Data maintained for optimization:
        # The most recently rendered camera position.
        self._recent_camera_position = ()
        # The most recently rendered camera rotation.
        self._recent_camera_rotation = ()
        # The most recently completed request id
        self._recent_complete_request_id = 0
        # The most recently accepted request id
        self._recent_accepted_request_id = -1
        # The data lock for avoiding race conditions
        self._data_lock = threading.Lock()

        # Construct camera config and renderer
        self.camera_config = RendererCameraConfig.load_config(camera_config_path)
        self.renderer = NerfStudioRenderer(model_config_path, eval_num_rays_per_chunk)

    def register_render_request(self, position, rotation, callback):
        """
        Registers a request to render with NerfStudioRenderer.

        Parameters
        ----------
        position : list[float]
            A 3-element list specifying the camera position.

        rotation : list[float]
            A 3-element list specifying the camera rotation, in euler angles.

        callback : function(np.array)
            A callback function to call when the renderer finishes this request.
        """
        # Increment the most recently accepted request id by 1
        with self._data_lock:
              self._recent_accepted_request_id += 1

        # Start a thread of this render request, with request id attached.
        renderer_call_args = (self._recent_accepted_request_id, position, rotation, callback)
        thread = threading.Thread(target=self._progressive_renderer_call, args=renderer_call_args)
        thread.start()
	
    def _progressive_renderer_call(self, request_id, position, rotation, callback):
        # For each render request, try to deliver the render output of the lowest quality fast.
        # When rendering of lower qualities are done, serially move to higher ones.
        for quality_index, config_entry in enumerate(self.camera_config):
            # For each config of different quality: obtain the rendered image, and then call the callback.
            image = self.renderer.render_at(position, rotation, config_entry['width'], config_entry['height'], config_entry['fov'])
            callback(image)
