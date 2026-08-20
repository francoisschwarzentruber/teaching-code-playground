import os
import subprocess
import tempfile
import uuid

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

COMPILE_TIMEOUT = 10
RUN_TIMEOUT = 10


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json()
    code = data.get("code", "")

    if not code.strip():
        return jsonify({"error": "No code provided.", "output": ""}), 400

    work_dir = tempfile.mkdtemp()
    job_id = uuid.uuid4().hex
    source_file = os.path.join(work_dir, f"{job_id}.c")
    binary_file = os.path.join(work_dir, job_id)

    try:
        with open(source_file, "w") as f:
            f.write(code)

        # Compile
        compile_result = subprocess.run(
            ["gcc", source_file, "-o", binary_file, "-lm"],
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
        )

        if compile_result.returncode != 0:
            return jsonify({"error": compile_result.stderr, "output": ""}), 400

        # Run
        run_result = subprocess.run(
            [binary_file],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            cwd=work_dir,
        )

        output = run_result.stdout
        error = run_result.stderr

        if run_result.returncode != 0:
            return jsonify({"error": error, "output": output}), 400

        return jsonify({"error": "", "output": output})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Execution timed out.", "output": ""}), 400
    except Exception as e:
        return jsonify({"error": str(e), "output": ""}), 500
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
