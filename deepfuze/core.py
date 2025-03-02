import os

os.environ['OMP_NUM_THREADS'] = '1'

import signal
import sys
import warnings
import shutil
import numpy
import onnxruntime
from time import sleep, time
from argparse import ArgumentParser, HelpFormatter

import deepfuze.choices
import deepfuze.globals
from deepfuze.face_analyser import get_one_face, get_average_face
from deepfuze.face_store import get_reference_faces, append_reference_face
from deepfuze import face_analyser, face_masker, content_analyser, config, process_manager, metadata, logger, wording, voice_extractor
from deepfuze.content_analyser import analyse_image, analyse_video
from deepfuze.processors.frame.core import get_frame_processors_modules, load_frame_processor_module
from deepfuze.common_helper import create_metavar, get_first
from deepfuze.execution import encode_execution_providers, decode_execution_providers
from deepfuze.normalizer import normalize_output_path, normalize_padding, normalize_fps
from deepfuze.memory import limit_system_memory
from deepfuze.statistics import conditional_log_statistics
from deepfuze.download import conditional_download
from deepfuze.filesystem import get_temp_frame_paths, get_temp_file_path, create_temp, move_temp, clear_temp, is_image, is_video, filter_audio_paths, resolve_relative_path, list_directory
from deepfuze.ffmpeg import extract_frames, merge_video, copy_image, finalize_image, restore_audio, replace_audio
from deepfuze.vision import read_image, read_static_images, detect_image_resolution, restrict_video_fps, create_image_resolutions, get_video_frame, detect_video_resolution, detect_video_fps, restrict_video_resolution, restrict_image_resolution, create_video_resolutions, pack_resolution, unpack_resolution

onnxruntime.set_default_logger_severity(3)
warnings.filterwarnings('ignore', category = UserWarning, module = 'gradio')


def cli() -> None:
	signal.signal(signal.SIGINT, lambda signal_number, frame: destroy())
	program = ArgumentParser(formatter_class = lambda prog: HelpFormatter(prog, max_help_position = 200), add_help = False)
	# general
	program.add_argument('-c', '--config', help = wording.get('help.config'), dest = 'config_path', default = 'deepfuze.ini')
	apply_config(program)
	program.add_argument('-s', '--source', help = wording.get('help.source'), action = 'append', dest = 'source_paths', default = config.get_str_list('general.source_paths'))
	program.add_argument('-t', '--target', help = wording.get('help.target'), dest = 'target_path', default = config.get_str_value('general.target_path'))
	program.add_argument('-o', '--output', help = wording.get('help.output'), dest = 'output_path', default = config.get_str_value('general.output_path'))
	program.add_argument('-v', '--version', version = metadata.get('name') + ' ' + metadata.get('version'), action = 'version')
	# misc
	group_misc = program.add_argument_group('misc')
	group_misc.add_argument('--force-download', help = wording.get('help.force_download'), action = 'store_true', default = config.get_bool_value('misc.force_download'))
	group_misc.add_argument('--skip-download', help = wording.get('help.skip_download'), action = 'store_true', default = config.get_bool_value('misc.skip_download'))
	group_misc.add_argument('--headless', help = wording.get('help.headless'), action = 'store_true', default = config.get_bool_value('misc.headless'))
	group_misc.add_argument('--log-level', help = wording.get('help.log_level'), default = config.get_str_value('misc.log_level', 'info'), choices = logger.get_log_levels())
	# execution
	execution_providers = encode_execution_providers(onnxruntime.get_available_providers())
	group_execution = program.add_argument_group('execution')
	group_execution.add_argument('--execution-device-id', help = wording.get('help.execution_device_id'), default = config.get_str_value('execution.face_detector_size', '0'))
	group_execution.add_argument('--execution-providers', help = wording.get('help.execution_providers').format(choices = ', '.join(execution_providers)), default = config.get_str_list('execution.execution_providers', 'cpu'), choices = execution_providers, nargs = '+', metavar = 'EXECUTION_PROVIDERS')
	group_execution.add_argument('--execution-thread-count', help = wording.get('help.execution_thread_count'), type = int, default = config.get_int_value('execution.execution_thread_count', '4'), choices = deepfuze.choices.execution_thread_count_range, metavar = create_metavar(deepfuze.choices.execution_thread_count_range))
	group_execution.add_argument('--execution-queue-count', help = wording.get('help.execution_queue_count'), type = int, default = config.get_int_value('execution.execution_queue_count', '1'), choices = deepfuze.choices.execution_queue_count_range, metavar = create_metavar(deepfuze.choices.execution_queue_count_range))
	# memory
	group_memory = program.add_argument_group('memory')
	group_memory.add_argument('--video-memory-strategy', help = wording.get('help.video_memory_strategy'), default = config.get_str_value('memory.video_memory_strategy', 'strict'), choices = deepfuze.choices.video_memory_strategies)
	group_memory.add_argument('--system-memory-limit', help = wording.get('help.system_memory_limit'), type = int, default = config.get_int_value('memory.system_memory_limit', '0'), choices = deepfuze.choices.system_memory_limit_range, metavar = create_metavar(deepfuze.choices.system_memory_limit_range))
	# face analyser
	group_face_analyser = program.add_argument_group('face analyser')
	group_face_analyser.add_argument('--face-analyser-order', help = wording.get('help.face_analyser_order'), default = config.get_str_value('face_analyser.face_analyser_order', 'left-right'), choices = deepfuze.choices.face_analyser_orders)
	group_face_analyser.add_argument('--face-analyser-age', help = wording.get('help.face_analyser_age'), default = config.get_str_value('face_analyser.face_analyser_age'), choices = deepfuze.choices.face_analyser_ages)
	group_face_analyser.add_argument('--face-analyser-gender', help = wording.get('help.face_analyser_gender'), default = config.get_str_value('face_analyser.face_analyser_gender'), choices = deepfuze.choices.face_analyser_genders)
	group_face_analyser.add_argument('--face-detector-model', help = wording.get('help.face_detector_model'), default = config.get_str_value('face_analyser.face_detector_model', 'yoloface'), choices = deepfuze.choices.face_detector_set.keys())
	group_face_analyser.add_argument('--face-detector-size', help = wording.get('help.face_detector_size'), default = config.get_str_value('face_analyser.face_detector_size', '640x640'))
	group_face_analyser.add_argument('--face-detector-score', help = wording.get('help.face_detector_score'), type = float, default = config.get_float_value('face_analyser.face_detector_score', '0.5'), choices = deepfuze.choices.face_detector_score_range, metavar = create_metavar(deepfuze.choices.face_detector_score_range))
	group_face_analyser.add_argument('--face-landmarker-score', help = wording.get('help.face_landmarker_score'), type = float, default = config.get_float_value('face_analyser.face_landmarker_score', '0.5'), choices = deepfuze.choices.face_landmarker_score_range, metavar = create_metavar(deepfuze.choices.face_landmarker_score_range))
	# face selector
	group_face_selector = program.add_argument_group('face selector')
	group_face_selector.add_argument('--face-selector-mode', help = wording.get('help.face_selector_mode'), default = config.get_str_value('face_selector.face_selector_mode', 'reference'), choices = deepfuze.choices.face_selector_modes)
	group_face_selector.add_argument('--reference-face-position', help = wording.get('help.reference_face_position'), type = int, default = config.get_int_value('face_selector.reference_face_position', '0'))
	group_face_selector.add_argument('--reference-face-distance', help = wording.get('help.reference_face_distance'), type = float, default = config.get_float_value('face_selector.reference_face_distance', '0.6'), choices = deepfuze.choices.reference_face_distance_range, metavar = create_metavar(deepfuze.choices.reference_face_distance_range))
	group_face_selector.add_argument('--reference-frame-number', help = wording.get('help.reference_frame_number'), type = int, default = config.get_int_value('face_selector.reference_frame_number', '0'))
	# face mask
	group_face_mask = program.add_argument_group('face mask')
	group_face_mask.add_argument('--face-mask-types', help = wording.get('help.face_mask_types').format(choices = ', '.join(deepfuze.choices.face_mask_types)), default = config.get_str_list('face_mask.face_mask_types', 'box'), choices = deepfuze.choices.face_mask_types, nargs = '+', metavar = 'FACE_MASK_TYPES')
	group_face_mask.add_argument('--face-mask-blur', help = wording.get('help.face_mask_blur'), type = float, default = config.get_float_value('face_mask.face_mask_blur', '0.3'), choices = deepfuze.choices.face_mask_blur_range, metavar = create_metavar(deepfuze.choices.face_mask_blur_range))
	group_face_mask.add_argument('--face-mask-padding', help = wording.get('help.face_mask_padding'), type = int, default = config.get_int_list('face_mask.face_mask_padding', '0 0 0 0'), nargs = '+')
	group_face_mask.add_argument('--face-mask-regions', help = wording.get('help.face_mask_regions').format(choices = ', '.join(deepfuze.choices.face_mask_regions)), default = config.get_str_list('face_mask.face_mask_regions', ' '.join(deepfuze.choices.face_mask_regions)), choices = deepfuze.choices.face_mask_regions, nargs = '+', metavar = 'FACE_MASK_REGIONS')
	# frame extraction
	group_frame_extraction = program.add_argument_group('frame extraction')
	group_frame_extraction.add_argument('--trim-frame-start', help = wording.get('help.trim_frame_start'), type = int, default = deepfuze.config.get_int_value('frame_extraction.trim_frame_start'))
	group_frame_extraction.add_argument('--trim-frame-end',	help = wording.get('help.trim_frame_end'), type = int, default = deepfuze.config.get_int_value('frame_extraction.trim_frame_end'))
	group_frame_extraction.add_argument('--temp-frame-format', help = wording.get('help.temp_frame_format'), default = config.get_str_value('frame_extraction.temp_frame_format', 'png'), choices = deepfuze.choices.temp_frame_formats)
	group_frame_extraction.add_argument('--keep-temp', help = wording.get('help.keep_temp'), action = 'store_true',	default = config.get_bool_value('frame_extraction.keep_temp'))
	# output creation
	group_output_creation = program.add_argument_group('output creation')
	group_output_creation.add_argument('--output-image-quality', help = wording.get('help.output_image_quality'), type = int, default = config.get_int_value('output_creation.output_image_quality', '80'), choices = deepfuze.choices.output_image_quality_range, metavar = create_metavar(deepfuze.choices.output_image_quality_range))
	group_output_creation.add_argument('--output-image-resolution', help = wording.get('help.output_image_resolution'), default = config.get_str_value('output_creation.output_image_resolution'))
	group_output_creation.add_argument('--output-video-encoder', help = wording.get('help.output_video_encoder'), default = config.get_str_value('output_creation.output_video_encoder', 'libx264'), choices = deepfuze.choices.output_video_encoders)
	group_output_creation.add_argument('--output-video-preset', help = wording.get('help.output_video_preset'), default = config.get_str_value('output_creation.output_video_preset', 'veryfast'), choices = deepfuze.choices.output_video_presets)
	group_output_creation.add_argument('--output-video-quality', help = wording.get('help.output_video_quality'), type = int, default = config.get_int_value('output_creation.output_video_quality', '80'), choices = deepfuze.choices.output_video_quality_range, metavar = create_metavar(deepfuze.choices.output_video_quality_range))
	group_output_creation.add_argument('--output-video-resolution', help = wording.get('help.output_video_resolution'), default = config.get_str_value('output_creation.output_video_resolution'))
	group_output_creation.add_argument('--output-video-fps', help = wording.get('help.output_video_fps'), type = float, default = config.get_str_value('output_creation.output_video_fps'))
	group_output_creation.add_argument('--skip-audio', help = wording.get('help.skip_audio'), action = 'store_true', default = config.get_bool_value('output_creation.skip_audio'))
	# frame processors
	available_frame_processors = list_directory('deepfuze/processors/frame/modules')
	program = ArgumentParser(parents = [ program ], formatter_class = program.formatter_class, add_help = True)
	group_frame_processors = program.add_argument_group('frame processors')
	group_frame_processors.add_argument('--frame-processors', help = wording.get('help.frame_processors').format(choices = ', '.join(available_frame_processors)), default = config.get_str_list('frame_processors.frame_processors', 'face_swapper'), nargs = '+')
	for frame_processor in available_frame_processors:
		frame_processor_module = load_frame_processor_module(frame_processor)
		frame_processor_module.register_args(group_frame_processors)
	# uis
	available_ui_layouts = list_directory('deepfuze/uis/layouts')
	group_uis = program.add_argument_group('uis')
	group_uis.add_argument('--open-browser', help=wording.get('help.open_browser'), action = 'store_true', default = config.get_bool_value('uis.open_browser'))
	group_uis.add_argument('--ui-layouts', help = wording.get('help.ui_layouts').format(choices = ', '.join(available_ui_layouts)), default = config.get_str_list('uis.ui_layouts', 'default'), nargs = '+')
	return run(program)


def apply_config(program : ArgumentParser) -> None:
	known_args = program.parse_known_args()
	deepfuze.globals.config_path = get_first(known_args).config_path


def validate_args(program : ArgumentParser) -> None:
	try:
		for action in program._actions:
			if action.default:
				if isinstance(action.default, list):
					for default in action.default:
						program._check_value(action, default)
				else:
					program._check_value(action, action.default)
	except Exception as exception:
		program.error(str(exception))


def apply_args(program : ArgumentParser) -> None:
	args = program.parse_args()
	# general
	deepfuze.globals.source_paths = args.source_paths
	deepfuze.globals.target_path = args.target_path
	deepfuze.globals.output_path = args.output_path
	# misc
	deepfuze.globals.force_download = args.force_download
	deepfuze.globals.skip_download = args.skip_download
	deepfuze.globals.headless = args.headless
	deepfuze.globals.log_level = args.log_level
	# execution
	deepfuze.globals.execution_device_id = args.execution_device_id
	deepfuze.globals.execution_providers = decode_execution_providers(args.execution_providers)
	deepfuze.globals.execution_thread_count = args.execution_thread_count
	deepfuze.globals.execution_queue_count = args.execution_queue_count
	# memory
	deepfuze.globals.video_memory_strategy = args.video_memory_strategy
	deepfuze.globals.system_memory_limit = args.system_memory_limit
	# face analyser
	deepfuze.globals.face_analyser_order = args.face_analyser_order
	deepfuze.globals.face_analyser_age = args.face_analyser_age
	deepfuze.globals.face_analyser_gender = args.face_analyser_gender
	deepfuze.globals.face_detector_model = args.face_detector_model
	if args.face_detector_size in deepfuze.choices.face_detector_set[args.face_detector_model]:
		deepfuze.globals.face_detector_size = args.face_detector_size
	else:
		deepfuze.globals.face_detector_size = '640x640'
	deepfuze.globals.face_detector_score = args.face_detector_score
	deepfuze.globals.face_landmarker_score = args.face_landmarker_score
	# face selector
	deepfuze.globals.face_selector_mode = args.face_selector_mode
	deepfuze.globals.reference_face_position = args.reference_face_position
	deepfuze.globals.reference_face_distance = args.reference_face_distance
	deepfuze.globals.reference_frame_number = args.reference_frame_number
	# face mask
	deepfuze.globals.face_mask_types = args.face_mask_types
	deepfuze.globals.face_mask_blur = args.face_mask_blur
	deepfuze.globals.face_mask_padding = normalize_padding(args.face_mask_padding)
	deepfuze.globals.face_mask_regions = args.face_mask_regions
	# frame extraction
	deepfuze.globals.trim_frame_start = args.trim_frame_start
	deepfuze.globals.trim_frame_end = args.trim_frame_end
	deepfuze.globals.temp_frame_format = args.temp_frame_format
	deepfuze.globals.keep_temp = args.keep_temp
	# output creation
	deepfuze.globals.output_image_quality = args.output_image_quality
	if is_image(args.target_path):
		output_image_resolution = detect_image_resolution(args.target_path)
		output_image_resolutions = create_image_resolutions(output_image_resolution)
		if args.output_image_resolution in output_image_resolutions:
			deepfuze.globals.output_image_resolution = args.output_image_resolution
		else:
			deepfuze.globals.output_image_resolution = pack_resolution(output_image_resolution)
	deepfuze.globals.output_video_encoder = args.output_video_encoder
	deepfuze.globals.output_video_preset = args.output_video_preset
	deepfuze.globals.output_video_quality = args.output_video_quality
	if is_video(args.target_path):
		output_video_resolution = detect_video_resolution(args.target_path)
		output_video_resolutions = create_video_resolutions(output_video_resolution)
		if args.output_video_resolution in output_video_resolutions:
			deepfuze.globals.output_video_resolution = args.output_video_resolution
		else:
			deepfuze.globals.output_video_resolution = pack_resolution(output_video_resolution)
	if args.output_video_fps or is_video(args.target_path):
		deepfuze.globals.output_video_fps = normalize_fps(args.output_video_fps) or detect_video_fps(args.target_path)
	deepfuze.globals.skip_audio = args.skip_audio
	# frame processors
	available_frame_processors = list_directory('deepfuze/processors/frame/modules')
	deepfuze.globals.frame_processors = args.frame_processors
	for frame_processor in available_frame_processors:
		frame_processor_module = load_frame_processor_module(frame_processor)
		frame_processor_module.apply_args(program)
	# uis
	deepfuze.globals.open_browser = args.open_browser
	deepfuze.globals.ui_layouts = args.ui_layouts


def run(program : ArgumentParser) -> int:
	validate_args(program)
	apply_args(program)
	logger.init(deepfuze.globals.log_level)

	if deepfuze.globals.system_memory_limit > 0:
		limit_system_memory(deepfuze.globals.system_memory_limit)
	if deepfuze.globals.force_download:
		force_download()
		return
	if not pre_check() or not content_analyser.pre_check() or not face_analyser.pre_check() or not face_masker.pre_check() or not voice_extractor.pre_check():
		print("pre_check failed")
		return -1
	for frame_processor_module in get_frame_processors_modules(deepfuze.globals.frame_processors):
		if not frame_processor_module.pre_check():
			print("pre_check failed")
			return -1
	if deepfuze.globals.headless:
		conditional_process()
	else:
		import deepfuze.uis.core as ui

		for ui_layout in ui.get_ui_layouts_modules(deepfuze.globals.ui_layouts):
			if not ui_layout.pre_check():
				print("pre_check failed")
				return -1
		ui.launch()
	return 0

def destroy() -> None:
	process_manager.stop()
	while process_manager.is_processing():
		sleep(0.5)
	if deepfuze.globals.target_path:
		clear_temp(deepfuze.globals.target_path)
	sys.exit(0)


def pre_check() -> bool:
	if sys.version_info < (3, 9):
		logger.error(wording.get('python_not_supported').format(version = '3.9'), __name__.upper())
		return False
	if not shutil.which('ffmpeg'):
		logger.error(wording.get('ffmpeg_not_installed'), __name__.upper())
		return False
	return True


def conditional_process() -> None:
	start_time = time()
	for frame_processor_module in get_frame_processors_modules(deepfuze.globals.frame_processors):
		while not frame_processor_module.post_check():
			logger.disable()
			sleep(0.5)
		logger.enable()
		if not frame_processor_module.pre_process('output'):
			return
	conditional_append_reference_faces()
	if is_image(deepfuze.globals.target_path):
		process_image(start_time)
	if is_video(deepfuze.globals.target_path):
		process_video(start_time)


def conditional_append_reference_faces() -> None:
	if 'reference' in deepfuze.globals.face_selector_mode and not get_reference_faces():
		source_frames = read_static_images(deepfuze.globals.source_paths)
		source_face = get_average_face(source_frames)
		if is_video(deepfuze.globals.target_path):
			reference_frame = get_video_frame(deepfuze.globals.target_path, deepfuze.globals.reference_frame_number)
		else:
			reference_frame = read_image(deepfuze.globals.target_path)
		reference_face = get_one_face(reference_frame, deepfuze.globals.reference_face_position)
		append_reference_face('origin', reference_face)
		if source_face and reference_face:
			for frame_processor_module in get_frame_processors_modules(deepfuze.globals.frame_processors):
				abstract_reference_frame = frame_processor_module.get_reference_frame(source_face, reference_face, reference_frame)
				if numpy.any(abstract_reference_frame):
					reference_frame = abstract_reference_frame
					reference_face = get_one_face(reference_frame, deepfuze.globals.reference_face_position)
					append_reference_face(frame_processor_module.__name__, reference_face)


def force_download() -> None:
	download_directory_path = resolve_relative_path('../../../models/deepfuze')
	available_frame_processors = list_directory('deepfuze/processors/frame/modules')
	model_list =\
	[
		content_analyser.MODELS,
		face_analyser.MODELS,
		face_masker.MODELS,
		voice_extractor.MODELS
	]

	for frame_processor_module in get_frame_processors_modules(available_frame_processors):
		if hasattr(frame_processor_module, 'MODELS'):
			model_list.append(frame_processor_module.MODELS)
	model_urls = [ models[model].get('url') for models in model_list for model in models ]

	conditional_download(download_directory_path, model_urls)


def process_image(start_time : float) -> None:
	normed_output_path = normalize_output_path(deepfuze.globals.target_path, deepfuze.globals.output_path)
	if analyse_image(deepfuze.globals.target_path):
		return
	# clear temp
	logger.debug(wording.get('clearing_temp'), __name__.upper())
	clear_temp(deepfuze.globals.target_path)
	# create temp
	logger.debug(wording.get('creating_temp'), __name__.upper())
	create_temp(deepfuze.globals.target_path)
	# copy image
	process_manager.start()
	temp_image_resolution = pack_resolution(restrict_image_resolution(deepfuze.globals.target_path, unpack_resolution(deepfuze.globals.output_image_resolution)))
	logger.info(wording.get('copying_image').format(resolution = temp_image_resolution), __name__.upper())
	if copy_image(deepfuze.globals.target_path, temp_image_resolution):
		logger.debug(wording.get('copying_image_succeed'), __name__.upper())
	else:
		logger.error(wording.get('copying_image_failed'), __name__.upper())
		return
	# process image
	temp_file_path = get_temp_file_path(deepfuze.globals.target_path)
	for frame_processor_module in get_frame_processors_modules(deepfuze.globals.frame_processors):
		logger.info(wording.get('processing'), frame_processor_module.NAME)
		frame_processor_module.process_image(deepfuze.globals.source_paths, temp_file_path, temp_file_path)
		frame_processor_module.post_process()
	if is_process_stopping():
		return
	# finalize image
	logger.info(wording.get('finalizing_image').format(resolution = deepfuze.globals.output_image_resolution), __name__.upper())
	if finalize_image(deepfuze.globals.target_path, normed_output_path, deepfuze.globals.output_image_resolution):
		logger.debug(wording.get('finalizing_image_succeed'), __name__.upper())
	else:
		logger.warn(wording.get('finalizing_image_skipped'), __name__.upper())
	# clear temp
	logger.debug(wording.get('clearing_temp'), __name__.upper())
	clear_temp(deepfuze.globals.target_path)
	# validate image
	if is_image(normed_output_path):
		seconds = '{:.2f}'.format((time() - start_time) % 60)
		logger.info(wording.get('processing_image_succeed').format(seconds = seconds), __name__.upper())
		conditional_log_statistics()
	else:
		logger.error(wording.get('processing_image_failed'), __name__.upper())
	process_manager.end()


def process_video(start_time : float) -> None:
	normed_output_path = normalize_output_path(deepfuze.globals.target_path, deepfuze.globals.output_path)
	if analyse_video(deepfuze.globals.target_path, deepfuze.globals.trim_frame_start, deepfuze.globals.trim_frame_end):
		return
	# clear temp
	logger.debug(wording.get('clearing_temp'), __name__.upper())
	clear_temp(deepfuze.globals.target_path)
	# create temp
	logger.debug(wording.get('creating_temp'), __name__.upper())
	create_temp(deepfuze.globals.target_path)
	# extract frames
	process_manager.start()
	temp_video_resolution = pack_resolution(restrict_video_resolution(deepfuze.globals.target_path, unpack_resolution(deepfuze.globals.output_video_resolution)))
	temp_video_fps = restrict_video_fps(deepfuze.globals.target_path, deepfuze.globals.output_video_fps)
	logger.info(wording.get('extracting_frames').format(resolution = temp_video_resolution, fps = temp_video_fps), __name__.upper())
	if extract_frames(deepfuze.globals.target_path, temp_video_resolution, temp_video_fps):
		logger.debug(wording.get('extracting_frames_succeed'), __name__.upper())
	else:
		if is_process_stopping():
			return
		logger.error(wording.get('extracting_frames_failed'), __name__.upper())
		return
	# process frames
	temp_frame_paths = get_temp_frame_paths(deepfuze.globals.target_path)
	if temp_frame_paths:
		for frame_processor_module in get_frame_processors_modules(deepfuze.globals.frame_processors):
			logger.info(wording.get('processing'), frame_processor_module.NAME)
			frame_processor_module.process_video(deepfuze.globals.source_paths, temp_frame_paths)
			frame_processor_module.post_process()
		if is_process_stopping():
			return
	else:
		logger.error(wording.get('temp_frames_not_found'), __name__.upper())
		return
	# merge video
	logger.info(wording.get('merging_video').format(resolution = deepfuze.globals.output_video_resolution, fps = deepfuze.globals.output_video_fps), __name__.upper())
	if merge_video(deepfuze.globals.target_path, deepfuze.globals.output_video_resolution, deepfuze.globals.output_video_fps):
		logger.debug(wording.get('merging_video_succeed'), __name__.upper())
	else:
		if is_process_stopping():
			return
		logger.error(wording.get('merging_video_failed'), __name__.upper())
		return
	# handle audio
	if deepfuze.globals.skip_audio:
		logger.info(wording.get('skipping_audio'), __name__.upper())
		move_temp(deepfuze.globals.target_path, normed_output_path)
	else:
		if 'lip_syncer' in deepfuze.globals.frame_processors:
			source_audio_path = get_first(filter_audio_paths(deepfuze.globals.source_paths))
			if source_audio_path and replace_audio(deepfuze.globals.target_path, source_audio_path, normed_output_path):
				logger.debug(wording.get('restoring_audio_succeed'), __name__.upper())
			else:
				if is_process_stopping():
					return
				logger.warn(wording.get('restoring_audio_skipped'), __name__.upper())
				move_temp(deepfuze.globals.target_path, normed_output_path)
		else:
			if restore_audio(deepfuze.globals.target_path, normed_output_path, deepfuze.globals.output_video_fps):
				logger.debug(wording.get('restoring_audio_succeed'), __name__.upper())
			else:
				if is_process_stopping():
					return
				logger.warn(wording.get('restoring_audio_skipped'), __name__.upper())
				move_temp(deepfuze.globals.target_path, normed_output_path)
	# clear temp
	logger.debug(wording.get('clearing_temp'), __name__.upper())
	clear_temp(deepfuze.globals.target_path)
	# validate video
	if is_video(normed_output_path):
		seconds = '{:.2f}'.format((time() - start_time))
		logger.info(wording.get('processing_video_succeed').format(seconds = seconds), __name__.upper())
		conditional_log_statistics()
	else:
		logger.error(wording.get('processing_video_failed'), __name__.upper())
	process_manager.end()


def is_process_stopping() -> bool:
	if process_manager.is_stopping():
		process_manager.end()
		logger.info(wording.get('processing_stopped'), __name__.upper())
	return process_manager.is_pending()
