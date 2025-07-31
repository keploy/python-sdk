import socket
import threading
import os
import json
import logging
import coverage

# --- Configuration ---
# Set up logging to be informative
logging.basicConfig(level=logging.INFO, format='[Keploy Agent] %(asctime)s - %(levelname)s - %(message)s')

# Define socket paths, same as the Go implementation
CONTROL_SOCKET_PATH = "/tmp/coverage_control.sock"
DATA_SOCKET_PATH = "/tmp/coverage_data.sock"

# --- Global State ---
# This lock protects access to the current_test_id
control_lock = threading.Lock()
# Stores the ID of the test case currently being recorded
current_test_id = None

cov = coverage.Coverage(data_file=None, auto_data=True)


def handle_control_request(conn: socket.socket):
    """
    Parses commands from Keploy ("START testID", "END testID") sent over the socket.
    This runs in its own thread for each connection.
    """
    global current_test_id
    try:
        with conn:
            reader = conn.makefile('r')
            command = reader.readline()
            if not command:
                return

            parts = command.strip().split(" ", 1)
            if len(parts) != 2:
                logging.error(f"Invalid command format: '{command.strip()}'")
                return

            action, test_id = parts[0], parts[1]

            with control_lock:
                if action == "START":
                    logging.info(f"Received START for test: {test_id}")
                    current_test_id = test_id
                    cov.erase()
                    cov.start()
                
                elif action == "END":
                    if current_test_id != test_id:
                        logging.warning(
                            f"Mismatched END command. Expected '{current_test_id}', got '{test_id}'. "
                            "Skipping coverage report."
                        )
                        return
                    
                    logging.info(f"Received END for test: {test_id}. Reporting coverage.")
                    cov.stop()
                    cov.save()
                    
                    try:
                        report_coverage(test_id)
                    except Exception as e:
                        logging.error(f"Failed to report coverage for test {test_id}: {e}", exc_info=True)
                    
                    current_test_id = None
                    # Acknowledge the command
                    conn.sendall(b"ACK\n")

                else:
                    logging.warning(f"Unrecognized command: {action}")

    except Exception as e:
        logging.error(f"Error handling control request: {e}", exc_info=True)


def report_coverage(test_id: str):
    """
    Gathers, processes, and sends the coverage data to the data socket.
    """
    data = cov.get_data()
    if not data:
        logging.warning("Coverage data is empty. No report will be sent.")
        return

    executed_lines_by_file = {}
    for filename in data.measured_files():
        abs_path = os.path.abspath(filename)
        lines = data.lines(filename)
        if lines:
            executed_lines_by_file[abs_path] = lines

    if not executed_lines_by_file:
        logging.warning(f"No covered lines were found for test {test_id}. The report will be empty.")
    
    payload = {
        "id": test_id,
        "executedLinesByFile": executed_lines_by_file,
    }

    try:
        json_data = json.dumps(payload).encode('utf-8')
        send_to_data_socket(json_data)
        logging.info(f"Successfully sent coverage report for test: {test_id}")
    except Exception as e:
        logging.error(f"Failed to serialize or send coverage data: {e}", exc_info=True)


def send_to_data_socket(data: bytes):
    """Connects to the Keploy data socket and writes the JSON payload."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(DATA_SOCKET_PATH)
            s.sendall(data)
    except Exception as e:
        logging.error(f"Could not connect or send to data socket at {DATA_SOCKET_PATH}: {e}")
        raise


def start_control_server():
    """
    Sets up and runs the Unix socket server that listens for commands from Keploy.
    This function runs in a background thread.
    """
    if os.path.exists(CONTROL_SOCKET_PATH):
        try:
            os.remove(CONTROL_SOCKET_PATH)
        except OSError as e:
            logging.error(f"Failed to remove old control socket: {e}")
            return

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(CONTROL_SOCKET_PATH)
        server.listen()
        logging.info(f"Control server listening on {CONTROL_SOCKET_PATH}")

        while True:
            conn, _ = server.accept()
            handler_thread = threading.Thread(target=handle_control_request, args=(conn,))
            handler_thread.start()

    except Exception as e:
        logging.error(f"Control server failed: {e}", exc_info=True)
    finally:
        server.close()
        logging.info("Control server shut down.")


# --- SIDE-EFFECT ON IMPORT ---
# This is the code that runs automatically when `import keploy_agent` is executed.
logging.info("Initializing...")

# Start the control server in a background daemon thread.
control_thread = threading.Thread(target=start_control_server, daemon=True)
control_thread.start()

logging.info("Agent initialized and control server started in the background.")
