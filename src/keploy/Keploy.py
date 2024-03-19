import requests
import logging
import threading
import subprocess
import time
import os

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("Keploy")

GRAPHQL_ENDPOINT = "/query"
HOST = "localhost"
PORT = 6789


class TestRunStatus:
    RUNNING = 1
    PASSED = 2
    FAILED = 3
    APP_HALTED = 4
    USER_ABORT = 5
    APP_FAULT = 6
    INTERNAL_ERR = 7


def get_test_run_status(status_str):
    status_mapping = {
        "RUNNING": TestRunStatus.RUNNING,
        "PASSED": TestRunStatus.PASSED,
        "FAILED": TestRunStatus.FAILED,
        "APP_HALTED": TestRunStatus.APP_HALTED,
        "USER_ABORT": TestRunStatus.USER_ABORT,
        "APP_FAULT": TestRunStatus.APP_FAULT,
        "INTERNAL_ERR": TestRunStatus.INTERNAL_ERR,
    }
    return status_mapping.get(status_str)


class RunOptions:
    def __init__(self, delay=10, debug=False, port=6789, path="."):
        if isinstance(delay, int) and delay >= 0:
            self.delay = delay
        else:
            raise ValueError("Delay must be a positive integer.")
        self.debug = debug
        if path != "":
            self.path = path
        # Ensure port is a positive integer
        if isinstance(port, int) and port >= 0:
            self.port = port
        else:
            raise ValueError("Port must be a positive integer.")


def run(run_cmd, run_options: RunOptions):
    if run_options.port != 0:
        global PORT
        PORT = run_options.port

    # Starting keploy
    start_keploy(run_cmd, run_options.delay, run_options.debug, PORT)

    # Delay for keploy to start
    time.sleep(5)

    try:
        # Fetching test sets
        test_sets, err = fetch_test_sets()
        if err is not None:
            raise AssertionError(f"error fetching test sets: {err}")

        logger.debug(f"Test sets found: {test_sets}")
        if len(test_sets) == 0:
            raise AssertionError("No test sets found. Are you in the right directory?")

        # Start hooking for the application
        appId, testRunId, err = start_hooks()
        if err is not None:
            raise AssertionError(f"error starting hooks: {err}")

        # Run for each test set.
        for test_set in test_sets:
            # Run test set
            run_test_set(testRunId, test_set, appId)
            # Start user application
            start_user_application(appId)

            path = os.path.abspath(run_options.path)
            report_path = os.path.join(
                path, "Keploy", "reports", testRunId, f"{test_set}-report.yaml"
            )
            # check if the report file is created
            err = check_report_file(report_path, run_options.delay + 10)
            if err is not None:
                # Stop user application
                appErr = stop_user_application(appId)
                if appErr is not None:
                    raise AssertionError(f"error stopping user application: {appErr}")
                logger.error(
                    f"error getting report file: {testRunId}, testSetId: {test_set}. Error: {err}"
                )
                continue

            # Wait for keploy to write initial data to report file

            logger.info(f"Running test set: {test_set} with testrun ID: {testRunId}")
            status = None
            while True:
                status, err = fetch_test_set_status(testRunId, test_set)
                if err is not None or status is None:
                    logger.error(
                        f"error getting test set status for testRunId: {testRunId}, testSetId: {test_set}. Error: {err}"
                    )
                    break

                match status:
                    case TestRunStatus.RUNNING:
                        logger.info(f"Test set: {test_set} is still running")
                    case TestRunStatus.PASSED:
                        break
                    case TestRunStatus.FAILED:
                        break
                    case TestRunStatus.APP_HALTED:
                        break
                    case TestRunStatus.USER_ABORT:
                        break
                    case TestRunStatus.APP_FAULT:
                        break
                    case TestRunStatus.INTERNAL_ERR:
                        break

            # Check if the test set status has some internal error
            if (
                status == None
                or status == TestRunStatus.APP_HALTED
                or status == TestRunStatus.USER_ABORT
                or status == TestRunStatus.APP_FAULT
                or status == TestRunStatus.INTERNAL_ERR
            ):
                logger.error(f"Test set: {test_set} failed with status: {status}")

            if status == TestRunStatus.FAILED:
                logger.error(f"Test set: {test_set} failed")
            elif status == TestRunStatus.PASSED:
                logger.info(f"Test set: {test_set} passed")

            # Stop user application
            err = stop_user_application(appId)
            if err is not None:
                raise AssertionError(f"error stopping user application: {err}")
            time.sleep(5)  # Wait for the user application to stop
    finally:
        # Stop keploy after running all test sets
        stop_Keploy()


def start_keploy(runCmd, delay, debug, port):
    thread = threading.Thread(
        target=run_keploy,
        args=(
            runCmd,
            delay,
            debug,
            port,
        ),
        daemon=False,
    )
    thread.start()
    return thread


def run_keploy(runCmd, delay, debug, port):
    overallCmd = f'sudo -E env "PATH=$PATH" /usr/local/bin/keploybin test -c "{runCmd}" --coverage --delay {delay} --port {port}'
    if debug:
        overallCmd += " --debug"

    logger.debug(f"Executing command: {overallCmd}")

    command = ["sh", "-c", overallCmd]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    # Read and print the output
    for line in process.stdout:
        # logger.info(line, end="")
        print(line, end="", flush=True)

    # Wait for the process to finish
    process.wait()


def set_http_client():
    try:
        url = f"http://{HOST}:{PORT}{GRAPHQL_ENDPOINT}"
        logger.debug(f"Connecting to: {url}")
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
        }
        session = requests.Session()
        return session, url, headers
    except Exception as e:
        logger.error(f"Error setting up HTTP client", e)
        return None, None, None


def fetch_test_sets():
    sessions, url, headers = set_http_client()
    if sessions is None or url is None or headers is None:
        return [], "Failed to set up HTTP client"

    payload = {"query": "{ testSets }"}

    try:
        response = sessions.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return [], res_body.get("errors", {})
            return res_body.get("data", {}).get("testSets"), None
        else:
            return [], None
    except Exception as e:
        logger.error("Error fetching test sets", e)
        return [], None


def start_hooks():
    session, url, headers = set_http_client()
    if session is None or url is None or headers is None:
        return None, None, "Failed to set up HTTP client"

    payload = {"query": "mutation StartHooks { startHooks { appId testRunId } }"}

    try:
        response = session.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return None, None, res_body.get("errors", {})

            start_hooks_data = res_body.get("data", {}).get("startHooks")
            if start_hooks_data is None:
                return None, None, f"Failed to get start Hooks data"

            appId = start_hooks_data.get("appId")
            testRunId = start_hooks_data.get("testRunId")
            return appId, testRunId, None
        else:
            return (
                None,
                None,
                f"Failed to start hooks. Status code: {response.status_code}",
            )
    except Exception as e:
        logger.error(f"Error starting hooks: {e}")
        return None, None, f"Error starting hooks: {e}"


def run_test_set(testRunId, testSetId, appId):
    session, url, headers = set_http_client()
    if session is None or url is None or headers is None:
        return False, "Failed to set up HTTP client"

    payload = {
        "query": f'mutation RunTestSet {{ runTestSet(testSetId: "{testSetId}", testRunId: "{testRunId}", appId: {appId}) }}'
    }

    try:
        response = session.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return False, res_body.get("errors", {})
            return res_body.get("data", {}).get("runTestSet"), None
        else:
            return False, f"Failed to run test set. Status code: {response.status_code}"
    except Exception as e:
        logger.error(f"Error running test set: {e}")
        return False, f"Error running test set: {e}"


def start_user_application(appId):
    session, url, headers = set_http_client()
    if session is None or url is None or headers is None:
        return False, "Failed to set up HTTP client"

    payload = {"query": f"mutation StartApp {{ startApp(appId: {appId}) }}"}

    try:
        response = session.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return False, res_body.get("errors", {})
            return res_body.get("data", {}).get("startApp"), None
        else:
            return (
                False,
                f"Failed to start user application. Status code: {response.status_code}",
            )
    except Exception as e:
        logger.error(f"Error starting user application: {e}")
        return False, f"Error starting user application: {e}"


def check_report_file(report_path, timeout):

    logger.debug(f"Checking report file at: {report_path}")
    print(f"Checking report file at: {report_path}")

    start_time = time.time()
    while time.time() - start_time < timeout:
        print(f"Checking Path: {report_path}")
        if os.path.exists(report_path):
            return None  # Report file found
        time.sleep(1)  # Wait for 1 second before checking again

    return f"Report file not created within {timeout} seconds"


def fetch_test_set_status(testRunId, testSetId):
    session, url, headers = set_http_client()
    if session is None or url is None or headers is None:
        return None, "Failed to set up HTTP client"

    payload = {
        "query": f'query GetTestSetStatus {{ testSetStatus(testRunId: "{testRunId}", testSetId: "{testSetId}") {{ status }} }}'
    }

    try:
        response = session.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return None, res_body.get("errors", {})
            test_set_status = res_body.get("data", {}).get("testSetStatus", {})
            status = test_set_status.get("status")
            return get_test_run_status(status), None
        else:
            return (
                None,
                f"Failed to fetch test set status. Status code: {response.status_code}",
            )
    except Exception as e:
        logger.error(f"Error fetching test set status: {e}")
        return None, f"Error fetching test set status: {e}"


def stop_user_application(appId):
    session, url, headers = set_http_client()
    if session is None or url is None or headers is None:
        return "Failed to set up HTTP client"

    payload = {"query": f"mutation StopApp {{ stopApp(appId: {appId}) }}"}

    try:
        response = session.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return res_body.get("errors", {})
            stop_app_result = res_body.get("data", {}).get("stopApp")
            logger.debug(f"stopApp result: {stop_app_result}")
        else:
            return (
                f"Failed to stop user application. Status code: {response.status_code}"
            )
    except Exception as e:
        logger.error(f"Error stopping user application: {e}")
        return f"Error stopping user application: {e}"

    return None


def stop_Keploy():
    session, url, headers = set_http_client()
    if session is None or url is None or headers is None:
        return "Failed to set up HTTP client"

    payload = {"query": "mutation { stopHooks }"}

    try:
        response = session.post(url, headers=headers, json=payload, timeout=10)
        logger.debug(f"Status code received: {response.status_code}")

        if response.ok:
            res_body = response.json()
            logger.debug(f"Response body received: {res_body}")
            if res_body.get("data", {}) is None:
                return res_body.get("errors", {})
            return res_body.get("data", {}).get("stopHooks")
    except Exception as e:
        logger.error(f"Error stopping hooks: {e}")
        return None
