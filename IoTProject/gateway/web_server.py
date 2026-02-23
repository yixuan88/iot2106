import logging
from flask import Flask, jsonify, request, abort, send_file
import io

from gateway import mesh_interface

logger = logging.getLogger(__name__)


def create_app(store, file_transfer): 
    app = Flask(__name__)

    @app.route("/")
    def index():  
        return app.send_static_file("index.html") if False else _render_index()

    def _render_index():
        from flask import render_template
        return render_template("index.html")

    @app.route("/api/messages", methods=["GET"])
    def get_messages():  # returns messages newer than since_id for polling
        try:
            since_id = int(request.args.get("since_id", 0))
        except ValueError:
            since_id = 0
        messages = store.get_all(since_id=since_id)
        return jsonify(messages)

    @app.route("/api/messages", methods=["POST"])
    def send_message():  # sends a text message over the mesh and logs it in the store
        body = request.get_json(silent=True)
        if not body or "text" not in body:
            abort(400, "Missing 'text' field")
        text = body["text"].strip()
        if not text:
            abort(400, "'text' must not be empty")
        destination = body.get("destination", "^all")
        mesh_interface.send_text(text, destination)
        msg = store.add_sent(text, destination)
        return jsonify(msg), 201

    @app.route("/api/nodes", methods=["GET"])
    def get_nodes():  # returns the list of known remote peer nodes
        nodes = mesh_interface.get_node_info()
        return jsonify(nodes)

    @app.route("/api/local-node", methods=["GET"])
    def get_local_node():  # returns info about the directly connected esp32, or null if not connected
        node = mesh_interface.get_local_node()
        if node is None:
            return jsonify(None)
        return jsonify(node)

    @app.route("/api/transfer/send", methods=["POST"])
    def transfer_send():  # accepts a file upload and starts a chunked send over the mesh
        if "file" not in request.files:
            abort(400, "No file part in request")
        f = request.files["file"]
        if f.filename == "":
            abort(400, "No file selected")
        destination = request.form.get("destination", "^all")
        file_bytes = f.read()
        try:
            transfer_id = file_transfer.send_file(file_bytes, f.filename, destination)
        except ValueError as e:
            abort(400, str(e))
        return jsonify({"transfer_id": transfer_id}), 202

    @app.route("/api/transfer/progress/<int:transfer_id>", methods=["GET"])
    def transfer_progress(transfer_id):  # returns the chunk progress of an active or completed transfer
        progress = file_transfer.get_progress(transfer_id)
        if progress is None:
            abort(404, "Transfer not found")
        return jsonify(progress)

    @app.route("/api/transfer/received", methods=["GET"])
    def transfer_received():  # lists all fully assembled inbound file transfers
        return jsonify(file_transfer.list_received())

    @app.route("/api/transfer/download/<int:transfer_id>", methods=["GET"])
    def transfer_download(transfer_id):  # downloads the raw bytes of a completed inbound transfer as a file
        data = file_transfer.get_received_data(transfer_id)
        if data is None:
            abort(404, "Transfer not found or not yet complete")
        return send_file(
            io.BytesIO(data),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"transfer_{transfer_id}.bin",
        )

    return app
