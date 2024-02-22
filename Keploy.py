import psutil
import os
import requests
import subprocess
import logging
import coverage
import time

logging.basicConfig(level=logging.info)

logger = logging.getLogger('Keploy')

GRAPHQL_ENDPOINT = "/query"
HOST = "http://localhost:"
server_port = 6789
user_command_pid = 0

class TestRunStatus:
    RUNNING = 1
    PASSED = 2
    FAILED = 3

def run(run_cmd):
        global user_command_pid
        test_sets = fetch_test_sets()
        logger.info("test_sets: ", test_sets)
        if len(test_sets) == 0:
            raise AssertionError("Failed to fetch test sets. Are you in the right directory?")
        # Run for each test set.
        for test_set in test_sets:
            start_user_application(run_cmd)
            test_run_id = run_test_set(test_set)
            startTime = time.time()
            if test_run_id is None:
                logger.error(f"Failed to run test set: {test_set}")
                continue
            logger.info(f"Running test set: {test_set} with testrun ID: {test_run_id}")
            while True:
                subprocess.call(["sleep", "2"])
                status = fetch_test_set_status(test_run_id)
                if status is None:
                    logger.error(f"Failed to fetch status for test set: {test_set}")
                if status == TestRunStatus.RUNNING:
                    logger.info(f"Test set: {test_set} is still running")
                elif status == TestRunStatus.PASSED:
                    logger.info(f"Test set: {test_set} passed")
                    break
                elif status == TestRunStatus.FAILED:
                    logger.error(f"Test set: {test_set} failed")
                    break
                # If 1 minute has passed we exit.
                if time.time() - startTime > 60:
                    logger.error(f"Test set: {test_set} took too long to run")
                    break
            find_coverage(test_set)
            stop_user_application()

            # Wait for the user application to stop
            subprocess.call(["sleep", "10"])

def start_user_application(run_cmd):
    global user_command_pid

    command = run_cmd.split(" ")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    user_command_pid = process.pid

def stop_user_application():
    kill_process_by_pid(user_command_pid)
    cov = coverage.Coverage()
    cov.load()
    cov.save()


def get_test_run_status(status_str):
    status_mapping = {
        'RUNNING': TestRunStatus.RUNNING,
        'PASSED': TestRunStatus.PASSED,
        'FAILED': TestRunStatus.FAILED
    }
    return status_mapping.get(status_str)

def set_http_client():
    try:
        url = f"{HOST}{server_port}{GRAPHQL_ENDPOINT}"
        logger.debug(f"Connecting to: {url}")
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json"
        }
        session = requests.Session()
        return session, url, headers
    except Exception as e:
        logger.error("Error setting up HTTP client", e)
        return None, None, None

def fetch_test_sets():
    sessions,url, headers = set_http_client()
    if url is None or headers is None:
        return None

    payload = {"query": "{ testSets }"}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            return res_body.get('data', {}).get('testSets')
    except Exception as e:
        logger.error("Error fetching test sets", e)
        return None

def fetch_test_set_status(test_run_id):
    try:
        session, url, headers = set_http_client()
        payload = {
            "query": f"{{ testSetStatus(testRunId: \"{test_run_id}\") {{ status }} }}"
        }

        response = session.post(url, headers=headers, json=payload)
        logger.debug(f"status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"response body received: {res_body}")
            status = res_body['data']['testSetStatus']['status']
            return get_test_run_status(status)
    except Exception as e:
        logger.error("Error fetching test set status", e)
        return None

def run_test_set(test_set_name):
    try:
        session, url, headers = set_http_client()
        payload = {
            "query": f"mutation {{ runTestSet(testSet: \"{test_set_name}\") {{ success testRunId message }} }}"
        }

        response = session.post(url, headers=headers, json=payload, timeout=5)  # Timeout set to 5 seconds
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if 'data' in res_body and 'runTestSet' in res_body['data']:
                return res_body['data']['runTestSet']['testRunId']
            else:
                logger.error(f"Unexpected response format: {res_body}")
                return None
    except Exception as e:
        logger.error("Error running test set", e)
        return None

def find_coverage(test_set):
    # Ensure the coverage-report directory exists
    coverage_report_dir = f'coverage-report/{test_set}'
    if not os.path.exists(coverage_report_dir):
        os.makedirs(coverage_report_dir)

    cov = coverage.Coverage()
    cov.load()

    # Generate text coverage report
    text_report_file = f"{coverage_report_dir}/{test_set}_coverage_report.txt"
    with open(text_report_file, 'w') as f:
        cov.report(file=f)

    # Generate HTML coverage report
    html_report_dir = f"{coverage_report_dir}/{test_set}_coverage_html"
    cov.html_report(directory=html_report_dir)

    # Log the report generation
    logger.info(f"Coverage reports generated in {coverage_report_dir}")

def kill_process_by_pid(pid):
    try:
        # Using SIGTERM (signal 15) to gracefully terminate the process
        subprocess.run(["kill", "-15", str(pid)])
        logger.debug(f"Killed process with PID: {pid}")
    except Exception as e:
        logger.error(f"Failed to kill process with PID {pid}", e)