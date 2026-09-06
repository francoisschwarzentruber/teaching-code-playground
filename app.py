import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import uuid

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

COMPILE_TIMEOUT = 10

sessions = {}
sessions_lock = threading.Lock()


def _start_session(binary_file, work_dir):
    """Launch the binary inside a pseudo-terminal and return a session dict."""
    master_fd, slave_fd = pty.openpty()

    def _child_setup():
        # Make the child a session leader with the pty as its controlling
        # terminal, so tty signals (Ctrl+C) and terminal modes work correctly.
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        # Reset keyboard signals to their default, in case the parent (e.g. a
        # background shell) ignored them; the pty will deliver them properly.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGQUIT, signal.SIG_DFL)
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)

    proc = subprocess.Popen(
        [binary_file],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=work_dir,
        close_fds=True,
        preexec_fn=_child_setup,
    )
    os.close(slave_fd)

    session = {
        "proc": proc,
        "master_fd": master_fd,
        "buffer": bytearray(),
        "done": False,
        "lock": threading.Lock(),
    }

    def reader():
        try:
            while True:
                r, _, _ = select.select([master_fd], [], [], 0.5)
                if master_fd in r:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    with session["lock"]:
                        session["buffer"] += data
                if session["proc"].poll() is not None:
                    # drain whatever is still left, then stop
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r:
                            break
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        with session["lock"]:
                            session["buffer"] += data
                    break
        except OSError:
            pass

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def waiter():
        proc.wait()
        reader_thread.join(timeout=2)
        with session["lock"]:
            session["done"] = True

    threading.Thread(target=waiter, daemon=True).start()
    return session


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_session():
    data = request.get_json()
    code = data.get("code", "")

    if not code.strip():
        return jsonify({"error": "No code provided."}), 400

    work_dir = tempfile.mkdtemp()
    job_id = uuid.uuid4().hex
    source_file = os.path.join(work_dir, f"{job_id}.c")
    binary_file = os.path.join(work_dir, job_id)

    try:
        with open(source_file, "w") as f:
            f.write(code)

        compile_result = subprocess.run(
            ["gcc", source_file, "-o", binary_file, "-lm"],
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
        )

        if compile_result.returncode != 0:
            return jsonify({"error": compile_result.stderr}), 400

        session = _start_session(binary_file, work_dir)
        session["work_dir"] = work_dir
        session["source_file"] = source_file
        session["binary_file"] = binary_file

        with sessions_lock:
            sessions[job_id] = session

        return jsonify({"job_id": job_id})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Compilation timed out."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for f in [source_file, binary_file]:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(work_dir)
        except OSError:
            pass


@app.route("/output", methods=["POST"])
def get_output():
    data = request.get_json()
    job_id = data.get("job_id", "")

    with sessions_lock:
        session = sessions.get(job_id)
    if not session:
        return jsonify({"error": "Session not found."}), 404

    proc = session["proc"]
    with session["lock"]:
        buf = bytes(session["buffer"])
        session["buffer"].clear()
        done = session["done"]
    exit_code = proc.returncode

    return jsonify(
        {
            "output": buf.decode("utf-8", errors="replace"),
            "done": done,
            "exit_code": exit_code,
        }
    )


@app.route("/input", methods=["POST"])
def send_input():
    data = request.get_json()
    job_id = data.get("job_id", "")
    payload = data.get("data", "")

    with sessions_lock:
        session = sessions.get(job_id)
    if not session:
        return jsonify({"error": "Session not found."}), 404

    proc = session["proc"]
    if proc.poll() is not None:
        return jsonify({"error": "Process has finished."}), 400

    try:
        os.write(session["master_fd"], payload.encode("utf-8"))
    except OSError as e:
        return jsonify({"error": "Could not send input: " + str(e)}), 400

    return jsonify({"ok": True})


@app.route("/resize", methods=["POST"])
def resize():
    data = request.get_json()
    job_id = data.get("job_id", "")
    cols = int(data.get("cols", 80))
    rows = int(data.get("rows", 24))

    with sessions_lock:
        session = sessions.get(job_id)
    if not session:
        return jsonify({"error": "Session not found."}), 404

    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(session["master_fd"], termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)