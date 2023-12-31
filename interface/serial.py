
import threading
import time

import serial
from flask import Blueprint, request, session
from serial.tools import list_ports

from interface import sio
from interface.kicad import local_to_board_coordinates, pads_data, dimensions_data

serial_bp = Blueprint("serial", __name__, url_prefix="/serial")

serial_connections = {}
serial_listeners = {}
serial_delays = {}
probe_offsets = {}

def create_device_listener(port):
    """
    Continuously listens for data from a serial device.

    This function reads data from a serial port and emits it to the client using a Socket.IO connection. It runs in an infinite loop until a `serial.SerialException` occurs.

    Returns:
        None
    """
    def _device_listener():
        """Continuously listens for data from a serial device"""
        # print(f"Listening for data from device on port {port}")
        while True:
            try:
                line = serial_connections[port].readline().decode().strip()
                sio.emit("command_response", {
                    "type": "received",
                    "data": line
                })
            except serial.SerialException:
                break

    thread = threading.Thread(target=_device_listener)
    thread.start()
    serial_listeners[port] = thread

def stop_device_listener(port):
    """Stops the device listener"""
    if port in serial_listeners:
        del serial_listeners[port]

@serial_bp.route("/connect_device", methods=["POST"])
def connect_device():
    """Connects to the serial port"""
    port = request.form.get("port")
    baudrate = request.form.get("baudrate")

    if port is not None:
        try:
            if port not in serial_connections:
                serial_connections[port] = serial.Serial(port, baudrate)

            elif not serial_connections[port].is_open:
                serial_connections[port].open()

            create_device_listener(port)

        except serial.SerialException:
            return "Port not available", 400

        session["port"] = port
        print(f"Connected to device on port {port}")
        session["baudrate"] = baudrate
    else:
        return "No port specified", 400

    sio.emit("device_connected", True)

    return "Connected", 200


@serial_bp.route("/disconnect_device", methods=["POST"])
def disconnect_device():
    """Disconnects from the serial port"""
    port = session.get("port")
    if port is not None and port in serial_connections:
        serial_connections[session["port"]].close()
        del serial_connections[session["port"]]
        del session["port"]
        del session["baudrate"]

    if port is not None and port in serial_listeners:
        stop_device_listener(port)

    sio.emit("device_connected", False)

    return "Disconnected", 200

@sio.on("get_devices_connection_status")
def get_devices_connection_status():
    """Check the status of the ports"""
    status = {}
    for port, device in serial_connections.items():
        status[port] = device.is_open

    sio.emit("devices_connection_status", status)


@sio.on("selected_serial_device")
def selected_serial_device(port):
    """Sets the selected serial device"""
    if port is not None and port in serial_connections:
        session["port"] = port


@sio.on("list_serial_devices")
def list_serial_devices():
    """Emits a list of available serial ports to the client"""
    ports = [port.device for port in list_ports.comports()]
    existing_ports = list(serial_connections.keys())
    for port in existing_ports:
        if port not in ports:
            del serial_connections[port]
    connected = []
    for port in ports:
        if port in serial_connections:
            connected.append(True)
        else:
            connected.append(False)

    devices = [{"port": port, "connected": connected} for port, connected in zip(ports, connected)]
    sio.emit("serial_devices", devices)


@serial_bp.route("/send_command", methods=["POST"])
def send_command():
    """Sends a command to the serial port"""
    command = request.form.get("command")
    port = session.get("port")

    if port is not None and port in serial_connections:
        if command.startswith("SETDELAY"):
            serial_delays[port] = int(command.split(" ")[1])
            return "Sent", 200
        else:
            _send_command(port, command.strip())
            return "Sent", 200
    else:
        sio.emit("command_response", {
            "type": "error",
            "data": f"Command error '{command}': Device not connected"
        })

        return "Device not connected", 400

def _send_command(port, command):
    if port not in serial_connections:
        return -1

    serial_connections[port].write((command + "\r\n") .encode())
    sio.emit("command_response", {
        "type": "sent",
        "data": command
    })


@serial_bp.route("/upload_command", methods=["POST"])
def upload_command():
    """Upload a file containing commands to be sent to the serial port"""
    port = session.get("port")

    if port is not None and port in serial_connections and serial_connections[port].is_open:
        if "command_file" in request.files:
            file = request.files["command_file"]
            file.save("commands.pcbpt")
            with open("commands.pcbpt", "r") as f:
                for command in f:
                    if command.startswith("SETDELAY"):
                        serial_delays[port] = int(command.split(" ")[1]) / 1000
                    else:
                        _send_command(port, command.strip())
                        time.sleep(serial_delays.get(port, 0.1))
        else:
            sio.emit("command_response", {
                "type": "error",
                "data": "File Upload Error: No file provided"
            })
            return "No file provided", 400
    else:
        sio.emit("command_response", {
            "type": "error",
            "data": "File Upload Error: Device not connected"
        })
        return "Device not connected", 400
    return "Sent", 200


@sio.on("update_probe_offsets")
def update_probe_offsets(offsets):
    """Updates the probe offsets"""
    session["probe_offsets"] = offsets
    if "port" in session:
        probe_offsets[session["port"]] = offsets


@sio.on("probe")
def probe(candidates):
    """Probes to the coordinates"""

    user_id = session.get("user_id")
    if user_id not in pads_data or user_id not in dimensions_data:
        sio.emit("command_response", {
            "type": "error",
            "data": "Command error: No file loaded"
        })

        return


    first = candidates["first"]
    second = candidates["second"]

    if first == "" or second == "":
        sio.emit("command_response", {
            "type": "error",
            "data": "Command error: No pads selected"
        })

        return

    x1, y1, _ = local_to_board_coordinates(pads_data[user_id][first], dimensions_data[user_id])
    x2, y2, _ = local_to_board_coordinates(pads_data[user_id][second], dimensions_data[user_id])

    if x1 > x2:
        x1, x2 = x2, x1
        y1, y2 = y2, y1

    if "probe_offsets" in session:
        x1 += float(session["probe_offsets"]["left"]["x"])
        y1 += float(session["probe_offsets"]["left"]["y"])
        x2 += float(session["probe_offsets"]["right"]["x"])
        y2 += float(session["probe_offsets"]["right"]["y"])
    elif "port" in session and session["port"] in probe_offsets:
        x1 += float(probe_offsets[session["port"]]["left"]["x"])
        y1 += float(probe_offsets[session["port"]]["left"]["y"])
        x2 += float(probe_offsets[session["port"]]["right"]["x"])
        y2 += float(probe_offsets[session["port"]]["right"]["y"])


    command = f"P A{x1:.3f} B{y1:.3f} X{x2:.3f} Y{y2:.3f}"

    if "port" not in session:
        sio.emit("command_response", {
            "type": "error",
            "data": f"Command error <br>'{command}': Device not connected"
        })

        return "Device not connected", 400

    print(command)

    _send_command(session["port"], command)
