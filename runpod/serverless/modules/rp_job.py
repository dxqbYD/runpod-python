"""
Job related helpers.
"""

import asyncio
import inspect
import json
import os
import traceback
from typing import Any, AsyncGenerator, Callable, Dict, Optional, Union, List

from runpod.http_client import ClientSession
from runpod.serverless.modules.rp_logger import RunPodLogger

from ...version import __version__ as runpod_version
from .rp_tips import check_return_size
from .worker_state import WORKER_ID, JobsQueue

JOB_GET_URL = str(os.environ.get("RUNPOD_WEBHOOK_GET_JOB")).replace("$ID", WORKER_ID)

log = RunPodLogger()
job_list = JobsQueue()


def _job_get_url(batch_size: int = 1):
    """
    Prepare the URL for making a 'get' request to the serverless API (sls).

    This function constructs the appropriate URL for sending a 'get' request to the serverless API,
    ensuring that the request will be correctly routed and processed by the API.

    Returns:
        str: The prepared URL for the 'get' request to the serverless API.
    """
    job_in_progress = "1" if job_list.get_job_count() else "0"

    if batch_size > 1:
        job_take_url = JOB_GET_URL.replace("/job-take/", "/job-take-batch/")
        job_take_url += f"&batch_size={batch_size}&batch_strategy=LMove"
    else:
        job_take_url = JOB_GET_URL

    return job_take_url + f"&job_in_progress={job_in_progress}"


async def get_job(
    session: ClientSession, num_jobs: int = 1
) -> Optional[List[Dict[str, Any]]]:
    """
    Get a job from the job-take API.

    `num_jobs = 1` will query the legacy singular job-take API.

    `num_jobs > 1` will query the batch job-take API.

    Args:
        session (ClientSession): The aiohttp ClientSession to use for the request.
        num_jobs (int): The number of jobs to get.
    """
    try:
        async with session.get(_job_get_url(num_jobs)) as response:
            if response.status == 204:
                log.debug("No content, no job to process.")
                return

            if response.status == 400:
                log.debug("Received 400 status, expected when FlashBoot is enabled.")
                return

            if response.status != 200:
                log.error(f"Failed to get job, status code: {response.status}")
                return

            jobs = await response.json()
            log.debug(f"Request Received | {jobs}")

            # legacy job-take API
            if isinstance(jobs, dict):
                if "id" not in jobs or "input" not in jobs:
                    raise Exception("Job has missing field(s): id or input.")
                return [jobs]

            # batch job-take API
            if isinstance(jobs, list):
                return jobs

    except asyncio.TimeoutError:
        log.debug("Timeout error, retrying.")

    except Exception as error:
        log.error(
            f"Failed to get job. | Error Type: {type(error).__name__} | Error Message: {str(error)}"
        )

    # empty
    return []


async def run_job(handler: Callable, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the job using the handler.

    Args:
        handler (Callable): The handler function to use.
        job (Dict[str, Any]): The job to run.

    Returns:
        Dict[str, Any]: The result of running the job.
    """
    log.info("Started.", job["id"])
    run_result = {}

    try:
        handler_return = handler(job)
        job_output = (
            await handler_return
            if inspect.isawaitable(handler_return)
            else handler_return
        )

        log.debug(f"Handler output: {job_output}", job["id"])

        if isinstance(job_output, dict):
            error_msg = job_output.pop("error", None)
            refresh_worker = job_output.pop("refresh_worker", None)
            run_result["output"] = job_output

            if error_msg:
                run_result["error"] = error_msg
            if refresh_worker:
                run_result["stopPod"] = True

        elif isinstance(job_output, bool):
            run_result = {"output": job_output}

        else:
            run_result = {"output": job_output}

        if run_result.get("output") == {}:
            run_result.pop("output")

        check_return_size(run_result)  # Checks the size of the return body.

    except Exception as err:
        error_info = {
            "error_type": str(type(err)),
            "error_message": str(err),
            "error_traceback": traceback.format_exc(),
            "hostname": os.environ.get("RUNPOD_POD_HOSTNAME", "unknown"),
            "worker_id": os.environ.get("RUNPOD_POD_ID", "unknown"),
            "runpod_version": runpod_version,
        }

        log.error("Captured Handler Exception", job["id"])
        log.error(json.dumps(error_info, indent=4))
        run_result = {"error": json.dumps(error_info)}

    finally:
        log.debug(f"run_job return: {run_result}", job["id"])

    return run_result


async def run_job_generator(
    handler: Callable, job: Dict[str, Any]
) -> AsyncGenerator[Dict[str, Union[str, Any]], None]:
    """
    Run generator job used to stream output.
    Yields output partials from the generator.
    """
    is_async_gen = inspect.isasyncgenfunction(handler)
    log.debug(
        "Using Async Generator" if is_async_gen else "Using Standard Generator",
        job["id"],
    )

    try:
        job_output = handler(job)

        if is_async_gen:
            async for output_partial in job_output:
                log.debug(f"Async Generator output: {output_partial}", job["id"])
                yield {"output": output_partial}
        else:
            for output_partial in job_output:
                log.debug(f"Generator output: {output_partial}", job["id"])
                yield {"output": output_partial}

    except Exception as err:
        log.error(err, job["id"])
        yield {"error": f"handler: {str(err)} \ntraceback: {traceback.format_exc()}"}
    finally:
        log.info("Finished running generator.", job["id"])
