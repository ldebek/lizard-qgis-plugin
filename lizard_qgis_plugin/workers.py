# Lizard plugin for QGIS, licensed under GPLv2 or (at your option) any later version
# Copyright (C) 2023 by Lutra Consulting for 3Di Water Management
import os
import time
from collections import defaultdict

import requests
from qgis.PyQt.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot
from threedi_mi_utils import bypass_max_path_limit

from lizard_qgis_plugin.utils import build_vrt, create_raster_tasks, split_scenario_extent


class ScenarioDownloadError(Exception):
    """Scenario files downloader exception class."""

    pass


class ScenarioItemsDownloaderSignals(QObject):
    """Definition of the download worker signals."""

    download_progress = pyqtSignal(dict, str, int, int)
    download_finished = pyqtSignal(dict, dict, str)
    download_failed = pyqtSignal(dict, str)


class ScenarioItemsDownloader(QRunnable):
    """Worker object responsible for downloading scenario files."""

    TASK_CHECK_SLEEP_TIME = 5

    def __init__(
        self, downloader, scenario_instance, raw_results_to_download, raster_results, download_dir, projection, no_data
    ):
        super().__init__()
        self.downloader = downloader
        self.scenario_instance = scenario_instance
        self.scenario_id = scenario_instance["uuid"]
        self.scenario_name = scenario_instance["name"]
        self.raw_results_to_download = raw_results_to_download
        self.raster_results = raster_results
        self.scenario_download_dir = os.path.join(download_dir, str(self.scenario_name))
        self.projection = projection
        self.no_data = no_data
        self.total_progress = 100
        self.current_step = 0
        self.number_of_steps = 0
        if raw_results_to_download:
            self.number_of_steps += len(raw_results_to_download)
        if raster_results:
            self.number_of_steps += len(self.raster_results) + 1  # Extra step for spawning raster creation tasks
        self.percentage_per_step = self.total_progress / self.number_of_steps
        self.signals = ScenarioItemsDownloaderSignals()
        self.downloaded_files = {}

    def download_raw_results(self):
        for result in self.raw_results_to_download:
            attachment_url = result["attachment_url"]
            attachment_filename = result["filename"]
            target_filepath = bypass_max_path_limit(os.path.join(self.scenario_download_dir, attachment_filename))
            progress_msg = f"Downloading '{attachment_filename}' (scenario: '{self.scenario_name}')..."
            self.report_progress(progress_msg)
            try:
                self.downloader.download_file(attachment_url, target_filepath)
            except Exception as e:
                error_msg = f"Download of the {attachment_filename} failed due to the following error: {e}"
                raise ScenarioDownloadError(error_msg)
            self.downloaded_files[attachment_filename] = target_filepath

    def download_raster_results(self):
        task_raster_results, processed_tasks = {}, {}
        success_statuses = {"SUCCESS"}
        in_progress_statuses = {"PENDING", "UNKNOWN", "STARTED", "RETRY"}
        # Create tasks
        progress_msg = f"Spawning raster tasks and preparing for download (scenario: '{self.scenario_name}')..."
        self.report_progress(progress_msg)
        spatial_bounds = split_scenario_extent(self.scenario_instance)
        for raster_result in self.raster_results:
            raster_url = raster_result["raster"]
            lizard_url = self.downloader.LIZARD_URL
            api_key = self.downloader.get_api_key()
            raster_response = requests.get(url=raster_url, auth=("__key__", api_key))
            raster_response.raise_for_status()
            raster = raster_response.json()
            original_raster_filename = raster_result["filename"]
            raster_name, raster_extension = original_raster_filename.rsplit(".", 1)
            tasks = create_raster_tasks(lizard_url, api_key, raster, spatial_bounds, self.projection, self.no_data)
            is_chunked_raster = len(tasks) > 1
            for raster_task_idx, task in enumerate(tasks, 1):
                task_id = task["task_id"]
                if is_chunked_raster:
                    raster_filename = f"{raster_name}_{raster_task_idx:02d}.{raster_extension}"
                    raster_result_copy = {k: v for k, v in raster_result.items()}
                    raster_result_copy["filename"] = raster_filename
                    task_raster_results[task_id] = raster_result_copy
                else:
                    task_raster_results[task_id] = raster_result
                processed_tasks[task_id] = False
        # Check status of task and download
        while not all(processed_tasks.values()):
            for task_id, processed in processed_tasks.items():
                if processed:
                    continue
                task_status = self.downloader.get_task_status(task_id)
                if task_status in success_statuses:
                    processed_tasks[task_id] = True
                elif task_status in in_progress_statuses:
                    continue
                else:
                    error_msg = f"Task {task_id} failed, status was: {task_status}"
                    raise ScenarioDownloadError(error_msg)
            time.sleep(self.TASK_CHECK_SLEEP_TIME)
        # Download tasks files
        rasters_per_code = defaultdict(list)
        for task_id, raster_result in sorted(task_raster_results.items(), key=lambda x: x[1]["filename"]):
            raster_filename = raster_result["filename"]
            raster_code = raster_result["code"]
            progress_msg = f"Downloading '{raster_filename}' (scenario: '{self.scenario_name}')..."
            self.report_progress(progress_msg, increase_current_step=raster_code not in rasters_per_code)
            raster_filepath = bypass_max_path_limit(os.path.join(self.scenario_download_dir, raster_filename))
            self.downloader.download_task(task_id, raster_filepath)
            rasters_per_code[raster_code].append(raster_filepath)
            self.downloaded_files[raster_filename] = raster_filepath
        self.report_progress(progress_msg, increase_current_step=False)
        vrt_options = {"resolution": "average", "resampleAlg": "nearest", "srcNodata": self.no_data}
        for raster_code, raster_filepaths in rasters_per_code.items():
            if len(raster_filepaths) > 1:
                raster_filepaths.sort()
                first_raster_filepath = raster_filepaths[0]
                vrt_filepath = first_raster_filepath.replace("_01", "").replace(".tif", ".vrt")
                vrt_filename = os.path.basename(vrt_filepath)
                progress_msg = f"Building VRT: '{vrt_filepath}'..."
                self.report_progress(progress_msg, increase_current_step=False)
                build_vrt(vrt_filepath, raster_filepaths, **vrt_options)
                self.downloaded_files[vrt_filename] = vrt_filepath
                for raster_filepath in raster_filepaths:
                    raster_filename = os.path.basename(raster_filepath)
                    del self.downloaded_files[raster_filename]

    @pyqtSlot()
    def run(self):
        """Downloading simulation results files."""
        try:
            self.report_progress(increase_current_step=False)
            if not os.path.exists(self.scenario_download_dir):
                os.makedirs(self.scenario_download_dir)
            self.download_raw_results()
            self.download_raster_results()
            self.report_finished(
                "Scenario items download finished. " f"Downloaded items are in: {self.scenario_download_dir}"
            )
        except ScenarioDownloadError as e:
            self.report_failure(str(e))
        except Exception as e:
            error_msg = f"Download failed due to the following error: {e}"
            self.report_failure(error_msg)

    def report_progress(self, progress_message=None, increase_current_step=True):
        """Report worker progress."""
        current_progress = int(self.current_step * self.percentage_per_step)
        if increase_current_step:
            self.current_step += 1
        self.signals.download_progress.emit(
            self.scenario_instance, progress_message, current_progress, self.total_progress
        )

    def report_failure(self, error_message):
        """Report worker failure message."""
        self.signals.download_failed.emit(self.scenario_instance, error_message)

    def report_finished(self, message):
        """Report worker finished message."""
        self.signals.download_finished.emit(self.scenario_instance, self.downloaded_files, message)
