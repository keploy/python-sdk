import json
import os
import requests
import subprocess
import threading
import logging
import coverage

logger = logging.getLogger('KeployCLI')

GRAPHQL_ENDPOINT = "/query"
HOST = "http://localhost:"
server_port = 6789
user_command_pid = 0

class TestRunStatus:
    RUNNING = 1
    PASSED = 2
    FAILED = 3

def start_user_application(run_cmd):
    global user_command_pid

    command = run_cmd.split(" ")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    user_command_pid = process.pid
    thread = threading.Thread(target=log_process_output, args=(process,))
    thread.start()
    process.wait()

def log_process_output(process):
    for line in iter(process.stdout.readline, ''):
        print(line, end='')

def run_keploy_server(pid, delay, test_path, port):

    server_port = port if port != 0 else server_port

    # command = [
    #     'sudo',
    #     '-S',
    #     '/usr/local/bin/keploy',
    #     'serve',
    #     f'--pid={pid}',
    #     f'-p={test_path}',
    #     f'-d={delay}',
    #     f'--port={server_port}',
    #     '--language="python"'
    # ]
    command = [
    'sudo',
    '-E',
    'keploy',
    'serve',
    '-c', 
    '"python3 app.py"',
    '--delay',
    '10',
    '--language=python'
    ]
    # logger.info(command)
    logger.info("Starting Keploy server...")
    try:
        process = subprocess.Popen(' '.join(command), shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(process)
        logger.info("Keploy server started, waiting for it to be ready...")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to start Keploy server: {str(e)}")

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
    
def kill_process_on_port(port):
    try:
        process = subprocess.Popen(["lsof", "-t", "-i:" + str(port)], stdout=subprocess.PIPE, universal_newlines=True)
        pids = process.communicate()[0]
        for pid_str in pids.split('\n'):
            if pid_str:
                kill_processes_and_their_children(int(pid_str.strip()))
    except Exception as e:
        logger.error("Failed to fetch the process ID on port " + str(port), e)

def kill_processes_and_their_children(parent_pid):
    try:
        pids = find_and_collect_child_processes(str(parent_pid))
        current_pid = os.getpid()
        for child_pid in pids:
            if child_pid != current_pid:
                subprocess.run(["kill", "-15", str(child_pid)])
                logger.debug(f"Killed child process {child_pid}")
    except Exception as e:
        logger.error("Failed to kill child process", e)

def find_and_collect_child_processes(parent_pid):
    pids = [int(parent_pid)]
    process = subprocess.Popen(["pgrep", "-P", parent_pid], stdout=subprocess.PIPE, universal_newlines=True)
    output = process.communicate()[0]
    for child_pid in output.split('\n'):
        if child_pid:
            pids.extend(find_and_collect_child_processes(child_pid))
    return pids


def stop_keploy_server():
    kill_process_on_port(server_port)

def kill_process_by_pid(pid):
    try:
        subprocess.run(["kill", "-15", str(pid)])
        logger.debug(f"Killed process with PID: {pid}")
    except Exception as e:
        logger.error(f"Failed to kill process with PID {pid}", e)

def stop_user_application() :
    kill_processes_and_their_children(user_command_pid)
