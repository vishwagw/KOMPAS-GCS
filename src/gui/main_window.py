"""
Main GUI Window for KOMPAS
Provides user interface for drone control and monitoring
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import time
import logging
from typing import Optional, Dict, Any

from communication.wifi_connection import DroneConnection
from control.drone_controller import DroneController, FlightMode
from utils.telemetry_parser import TelemetryParser
from utils.telemetry_logger import TelemetryLogger
from utils.safety_manager import SafetyManager, SafetyLevel
from utils.preflight import PreflightChecker, CheckStatus
from gui.video_window import VideoWindow

class DroneControllerGUI:
    """Main application window for drone control"""

    def __init__(self, config: dict, sim_drone=None):
        """
        Initialize GUI application

        Args:
            config: Configuration dictionary
            sim_drone: SimulatedDrone when in Flight Simulator Mode (enables fault injection)
        """
        self.config = config
        self.sim_drone = sim_drone
        self.safety: Optional[SafetyManager] = None
        self.preflight: Optional[PreflightChecker] = None
        self._takeoff_pending = False
        self.logger = logging.getLogger(__name__)

        # Core components
        self.connection: Optional[DroneConnection] = None
        self.controller: Optional[DroneController] = None
        self.telemetry_parser = TelemetryParser()
        self.video_window: Optional[VideoWindow] = None

        # GUI components
        self.root: Optional[tk.Tk] = None
        self.status_vars = {}
        self.control_buttons = {}
        self.video_status_var: Optional[tk.StringVar] = None

        # Status tracking
        self.is_connected = False
        self.last_telemetry_update = 0
        self.simulator_mode = bool(self.config.get('simulator', {}).get('enabled', False))
        self._telemetry_times = []      # arrival times over the last 2 s, for the rate readout
        self._last_telemetry_text = 0.0

        # Initialize GUI
        self.setup_gui()
        self._shutdown_complete = False
        self._shutdown_logger_done = False
        self._shutdown_connection_done = False
        self._shutdown_safety_done = False

    def on_closing(self):
        """Handle window close button click."""
        self._app_shutdown()
        self.root.destroy()

    def _stop_logging_on_disconnect(self):
        """Stop logging and disconnect, preserving safety monitoring.
        Safe to call multiple times; subsequent calls are no-ops for connection disconnect."""
        if self.telemetry_logger:
            self.telemetry_logger.stop_logging()
        if self.connection:
            self.connection.disconnect()

    def _app_shutdown(self):
        """Full application shutdown: stop logging, disconnect, stop safety.
        Idempotent per-operation: safe to call multiple times. Each cleanup operation
        is individually tracked; failed operations are retried on subsequent calls.
        Operations that succeed are not repeated."""
        # Track which operations still need to run
        need_logger_shutdown = self.telemetry_logger and not self._shutdown_logger_done
        need_connection_disconnect = self.connection and not self._shutdown_connection_done
        need_safety_stop = self.safety and not self._shutdown_safety_done

        if not (need_logger_shutdown or need_connection_disconnect or need_safety_stop):
            return  # Everything already done

        # Stop logging (retry if previously failed)
        if need_logger_shutdown:
            try:
                self.telemetry_logger.stop_logging()
                self._shutdown_logger_done = True
            except Exception as e:
                self.logger.error(f"Error stopping telemetry logger during shutdown: {e}")

        # Disconnect drone (retry if previously failed)
        if need_connection_disconnect:
            try:
                self.connection.disconnect()
                self._shutdown_connection_done = True
            except Exception as e:
                self.logger.error(f"Error disconnecting during shutdown: {e}")

        # Stop safety monitoring (retry if previously failed)
        if need_safety_stop:
            try:
                self.safety.stop_monitoring()
                self._shutdown_safety_done = True
            except Exception as e:
                self.logger.error(f"Error stopping safety monitoring during shutdown: {e}")

        # If all operations succeeded, mark overall shutdown complete
        # to prevent re-running on window-close + finally double-call
        if self._shutdown_logger_done and self._shutdown_connection_done and self._shutdown_safety_done:
            self._shutdown_complete = True

    def setup_gui(self):
        """Initialize the main GUI window"""
        self.root = tk.Tk()
        title = "KOMPAS ground control station"
        if self.simulator_mode:
            title += "  [FLIGHT SIMULATOR]"
        self.root.title(title)
        # 800x600 was too small: the telemetry panel collapsed to nothing and
        # the connection row was clipped. The minsize keeps every panel visible.
        self.root.geometry("1000x900")
        self.root.minsize(960, 860)
        self.root.resizable(True, True)

        # Set window icon (if available)
        try:
            self.root.iconbitmap("icon.ico")
        except:
            pass

        # Configure styles
        style = ttk.Style()
        style.theme_use('clam')

        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(2, weight=1)

        # Register WM_DELETE_WINDOW handler - cleanup before destruction
        self.root.protocol('WM_DELETE_WINDOW', self.on_closing)

        # Create GUI sections
        self.create_connection_section(main_frame)
        self.create_status_section(main_frame)
        self.create_control_section(main_frame)
        self.create_telemetry_section(main_frame)
        self.create_log_section(main_frame)

        # Setup key bindings
        self.setup_key_bindings()

        # Initialize connection
        self.initialize_connection()

        # Start GUI update loop
        self.update_gui()

        # In simulator mode there is nothing to set up by hand: connect at
        # once so telemetry is flowing as soon as the window appears.
        if self.simulator_mode:
            self.root.after(500, self.connect_drone)

    def create_connection_section(self, parent):
        """Create connection control section"""
        # Connection frame
        conn_frame = ttk.LabelFrame(parent, text="Connection", padding="5")
        conn_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)

        # Connection settings
        ttk.Label(conn_frame, text="Drone IP:").grid(row=0, column=0, padx=5)
        self.ip_var = tk.StringVar(value=self.config.get('network', {}).get('drone_ip', '192.168.1.1'))
        ip_entry = ttk.Entry(conn_frame, textvariable=self.ip_var, width=15)
        ip_entry.grid(row=0, column=1, padx=5)

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=2, padx=5)
        self.port_var = tk.StringVar(value=str(self.config.get('network', {}).get('command_port', 8889)))
        port_entry = ttk.Entry(conn_frame, textvariable=self.port_var, width=8)
        port_entry.grid(row=0, column=3, padx=5)

        # Connection buttons
        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self.connect_drone)
        self.connect_btn.grid(row=0, column=4, padx=10)

        self.disconnect_btn = ttk.Button(conn_frame, text="Disconnect",
                                       command=self.disconnect_drone, state='disabled')
        self.disconnect_btn.grid(row=0, column=5, padx=5)

        # Video window button
        self.video_btn = ttk.Button(conn_frame, text="Open Camera",
                                   command=self.toggle_video_window)
        self.video_btn.grid(row=0, column=6, padx=5)

        # Start Video button
        self.start_video_btn = ttk.Button(conn_frame, text="Start Video",
                                         command=self.start_video_with_hud)
        self.start_video_btn.grid(row=0, column=7, padx=5)

        # Video status indicator
        self.video_status_var = tk.StringVar(value="Video: Stopped")
        video_status_label = ttk.Label(conn_frame, textvariable=self.video_status_var,
                                     foreground="blue")
        video_status_label.grid(row=1, column=6, columnspan=2, padx=5, pady=2)

        # Simulator banner: impossible to miss, so a simulated session is
        # never mistaken for a real one.
        if self.simulator_mode:
            tk.Label(conn_frame, text=" FLIGHT SIMULATOR - no real aircraft ",
                     bg="#f0a020", fg="black", font=("TkDefaultFont", 9, "bold")
                     ).grid(row=1, column=0, columnspan=4, sticky=tk.W, padx=5, pady=2)

        # Fault injection, to rehearse the safety responses in simulation.
        if self.sim_drone is not None:
            self.link_fault_btn = ttk.Button(conn_frame, text="Cut link", command=self.toggle_sim_link)
            self.link_fault_btn.grid(row=1, column=4, padx=10, pady=2)
            ttk.Button(conn_frame, text="Battery 15%",
                       command=lambda: self.sim_drone.set_battery(15)).grid(row=1, column=5, padx=5, pady=2)

        # Connection status
        self.connection_status_var = tk.StringVar(value="Disconnected")
        status_label = ttk.Label(conn_frame, textvariable=self.connection_status_var,
                               foreground="red")
        status_label.grid(row=0, column=8, padx=10)

    def create_status_section(self, parent):
        """Create drone status display section"""
        left_column = ttk.Frame(parent)
        left_column.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N), pady=5)
        left_column.columnconfigure(0, weight=1)

        status_frame = ttk.LabelFrame(left_column, text="Drone Status", padding="5")
        status_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N))

        self.create_preflight_section(left_column)

        # Status variables
        self.status_vars = {
            'battery': tk.StringVar(value="--"),
            'altitude': tk.StringVar(value="--"),
            'speed': tk.StringVar(value="--"),
            'mode': tk.StringVar(value="--"),
            'flying': tk.StringVar(value="No"),
            'link': tk.StringVar(value="--")
        }

        # Status display
        row = 0
        for label, var in self.status_vars.items():
            ttk.Label(status_frame, text=f"{label.title()}:").grid(row=row, column=0,
                                                                 sticky=tk.W, padx=5)
            ttk.Label(status_frame, textvariable=var, width=10).grid(row=row, column=1,
                                                                   sticky=tk.W, padx=5)
            row += 1

        # Safety status: the most important active alert, colour-coded.
        self.safety_label = tk.Label(status_frame, text="Safety: OK", bg="#2e7d32", fg="white",
                                     anchor=tk.W, padx=6, wraplength=340, justify=tk.LEFT)
        self.safety_label.grid(row=row, column=0, columnspan=3, sticky=(tk.W, tk.E), padx=5, pady=(6, 2))

        # Emergency stop button
        self.emergency_btn = ttk.Button(status_frame, text="EMERGENCY STOP",
                                      command=self.emergency_stop,
                                      style='Emergency.TButton')
        self.emergency_btn.grid(row=0, column=2, rowspan=2, padx=20, pady=5)

        # Configure emergency button style
        style = ttk.Style()
        style.configure('Emergency.TButton', foreground='white', background='red')

    PREFLIGHT_CHECKS = ("Connection", "Telemetry link", "Command link", "Battery",
                        "Level", "Temperature", "On the ground", "Safety monitor")
    BADGES = {
        CheckStatus.PASS: ("PASS", "#2e7d32", "white"),
        CheckStatus.WARN: ("WARN", "#f9a825", "black"),
        CheckStatus.FAIL: ("FAIL", "#c62828", "white"),
    }

    def create_preflight_section(self, parent):
        """Pre-flight checklist: gates the Takeoff button."""
        frame = ttk.LabelFrame(parent, text="Pre-flight Checklist", padding="5")
        frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N), pady=(5, 0))
        frame.columnconfigure(2, weight=1)

        self.preflight_rows = {}
        for i, name in enumerate(self.PREFLIGHT_CHECKS):
            badge = tk.Label(frame, text="--", width=5, bg="#9e9e9e", fg="white",
                             font=("TkDefaultFont", 8, "bold"))
            badge.grid(row=i, column=0, padx=(2, 6), pady=1, sticky=tk.W)
            ttk.Label(frame, text=name).grid(row=i, column=1, sticky=tk.W)
            detail = ttk.Label(frame, text="", foreground="#555555")
            detail.grid(row=i, column=2, sticky=tk.W, padx=(8, 0))
            self.preflight_rows[name] = (badge, detail)

        self.preflight_summary = tk.Label(frame, text="Connect to begin checks", anchor=tk.W,
                                          bg="#9e9e9e", fg="white", padx=6)
        self.preflight_summary.grid(row=len(self.PREFLIGHT_CHECKS), column=0, columnspan=3,
                                    sticky=(tk.W, tk.E), pady=(6, 0))

    def _show_preflight(self, report):
        """Render a report into the checklist panel (UI thread)."""
        for result in report.results:
            row = self.preflight_rows.get(result.name)
            if not row:
                continue
            text, bg, fg = self.BADGES[result.status]
            row[0].config(text=text, bg=bg, fg=fg)
            row[1].config(text=result.detail)
        if report.failures:
            self.preflight_summary.config(
                text=f"TAKEOFF BLOCKED - {len(report.failures)} check(s) failing", bg="#c62828", fg="white")
        elif report.warnings:
            self.preflight_summary.config(
                text=f"READY - {len(report.warnings)} warning(s) to confirm", bg="#f9a825", fg="black")
        else:
            self.preflight_summary.config(text="READY FOR TAKEOFF", bg="#2e7d32", fg="white")

    def _set_preflight_idle(self, message: str, bg: str = "#9e9e9e"):
        for badge, detail in self.preflight_rows.values():
            badge.config(text="--", bg="#9e9e9e", fg="white")
            detail.config(text="")
        self.preflight_summary.config(text=message, bg=bg, fg="white")

    def _refresh_preflight(self):
        """Passive re-check on the ground; enables Takeoff only when clear."""
        if not self.preflight:
            return
        takeoff_btn = self.control_buttons['takeoff']
        if not self.is_connected:
            self._set_preflight_idle("Connect to begin checks")
            return
        if self.controller and self.controller.is_flying:
            # Ground checks mean nothing in flight (and "On the ground: FAIL"
            # in red would be alarming noise); in-flight limits are the safety
            # monitor's job.
            self._set_preflight_idle("Airborne - checks resume after landing", bg="#1565c0")
            return
        report = self.preflight.run()
        self._show_preflight(report)
        if not self._takeoff_pending:
            takeoff_btn.config(state='normal' if report.cleared else 'disabled')

    def create_control_section(self, parent):
        """Create flight control section"""
        control_frame = ttk.LabelFrame(parent, text="Flight Controls", padding="10")
        control_frame.grid(row=1, column=1, sticky=(tk.W, tk.E, tk.N), pady=5, padx=(10, 0))

        # Basic flight controls
        flight_frame = ttk.Frame(control_frame)
        flight_frame.grid(row=0, column=0, columnspan=3, pady=10)

        self.control_buttons['takeoff'] = ttk.Button(flight_frame, text="Takeoff",
                                                   command=self.takeoff_drone, state='disabled')
        self.control_buttons['takeoff'].grid(row=0, column=0, padx=5)

        self.control_buttons['land'] = ttk.Button(flight_frame, text="Land",
                                                command=self.land_drone, state='disabled')
        self.control_buttons['land'].grid(row=0, column=1, padx=5)

        # Movement controls
        move_frame = ttk.LabelFrame(control_frame, text="Movement", padding="5")
        move_frame.grid(row=1, column=0, columnspan=3, pady=10, sticky=(tk.W, tk.E))

        # Directional buttons
        self.control_buttons['forward'] = ttk.Button(move_frame, text="↑",
                                                   command=lambda: self.move_drone('forward'),
                                                   state='disabled', width=3)
        self.control_buttons['forward'].grid(row=0, column=1)

        self.control_buttons['left'] = ttk.Button(move_frame, text="←",
                                                command=lambda: self.move_drone('left'),
                                                state='disabled', width=3)
        self.control_buttons['left'].grid(row=1, column=0)

        self.control_buttons['right'] = ttk.Button(move_frame, text="→",
                                                 command=lambda: self.move_drone('right'),
                                                 state='disabled', width=3)
        self.control_buttons['right'].grid(row=1, column=2)

        self.control_buttons['backward'] = ttk.Button(move_frame, text="↓",
                                                    command=lambda: self.move_drone('backward'),
                                                    state='disabled', width=3)
        self.control_buttons['backward'].grid(row=2, column=1)

        # Vertical controls
        vert_frame = ttk.Frame(move_frame)
        vert_frame.grid(row=0, column=3, rowspan=3, padx=20)

        self.control_buttons['up'] = ttk.Button(vert_frame, text="Up",
                                              command=lambda: self.move_drone('up'),
                                              state='disabled', width=5)
        self.control_buttons['up'].grid(row=0, column=0, pady=2)

        self.control_buttons['down'] = ttk.Button(vert_frame, text="Down",
                                                command=lambda: self.move_drone('down'),
                                                state='disabled', width=5)
        self.control_buttons['down'].grid(row=1, column=0, pady=2)

        # Rotation controls
        rot_frame = ttk.Frame(move_frame)
        rot_frame.grid(row=1, column=1, padx=10)

        self.control_buttons['rotate_left'] = ttk.Button(rot_frame, text="↺",
                                                       command=lambda: self.rotate_drone('left'),
                                                       state='disabled', width=3)
        self.control_buttons['rotate_left'].grid(row=0, column=0, padx=2)

        self.control_buttons['rotate_right'] = ttk.Button(rot_frame, text="↻",
                                                        command=lambda: self.rotate_drone('right'),
                                                        state='disabled', width=3)
        self.control_buttons['rotate_right'].grid(row=0, column=1, padx=2)

        # Speed control
        speed_frame = ttk.Frame(control_frame)
        speed_frame.grid(row=2, column=0, columnspan=3, pady=10)

        ttk.Label(speed_frame, text="Speed:").grid(row=0, column=0)
        self.speed_var = tk.IntVar(value=50)
        speed_scale = ttk.Scale(speed_frame, from_=10, to=100, variable=self.speed_var,
                              orient=tk.HORIZONTAL, length=200, command=self.update_speed)
        speed_scale.grid(row=0, column=1, padx=10)

        self.speed_label = ttk.Label(speed_frame, text="50")
        self.speed_label.grid(row=0, column=2)

    def create_telemetry_section(self, parent):
        """Create telemetry display section"""
        telemetry_frame = ttk.LabelFrame(parent, text="Telemetry", padding="5")
        telemetry_frame.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)

        # Telemetry text area
        self.telemetry_text = scrolledtext.ScrolledText(telemetry_frame, height=8, width=80)
        self.telemetry_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        telemetry_frame.columnconfigure(0, weight=1)
        telemetry_frame.rowconfigure(0, weight=1)

    def create_log_section(self, parent):
        """Create log display section"""
        log_frame = ttk.LabelFrame(parent, text="Log", padding="5")
        log_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)

        # Log text area
        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, width=80)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

    def setup_key_bindings(self):
        """Setup keyboard shortcuts"""
        self.root.bind('<KeyPress-w>', lambda e: self.move_drone('forward') if self.is_connected else None)
        self.root.bind('<KeyPress-s>', lambda e: self.move_drone('backward') if self.is_connected else None)
        self.root.bind('<KeyPress-a>', lambda e: self.move_drone('left') if self.is_connected else None)
        self.root.bind('<KeyPress-d>', lambda e: self.move_drone('right') if self.is_connected else None)
        self.root.bind('<KeyPress-q>', lambda e: self.rotate_drone('left') if self.is_connected else None)
        self.root.bind('<KeyPress-e>', lambda e: self.rotate_drone('right') if self.is_connected else None)
        self.root.bind('<KeyPress-r>', lambda e: self.move_drone('up') if self.is_connected else None)
        self.root.bind('<KeyPress-f>', lambda e: self.move_drone('down') if self.is_connected else None)
        self.root.bind('<KeyPress-t>', lambda e: self.takeoff_drone() if self.is_connected else None)
        self.root.bind('<KeyPress-l>', lambda e: self.land_drone() if self.is_connected else None)
        self.root.bind('<KeyPress-space>', lambda e: self.emergency_stop())

        # Focus window to receive key events
        self.root.focus_set()

    def initialize_connection(self):
        """Initialize drone connection object"""
        drone_ip = self.config.get('network', {}).get('drone_ip', '192.168.1.1')
        command_port = self.config.get('network', {}).get('command_port', 8889)
        state_port = self.config.get('network', {}).get('state_port', 8890)

        self.connection = DroneConnection(drone_ip, command_port, state_port)
        self.connection.set_telemetry_callback(self.on_telemetry_received)
        self.connection.set_connection_callback(self.on_connection_changed)

        save_csv = bool(self.config.get('logging', {}).get('save_flight_csv', True))
        self.telemetry_logger = TelemetryLogger() if save_csv else None
        self.controller = DroneController(self.connection, telemetry_logger=self.telemetry_logger)
        # Limits come from the config, not the controller's built-in defaults.
        self.controller.min_battery = self.config.get('safety', {}).get('min_battery_level', 20)
        self.controller.max_altitude = self.config.get('flight', {}).get('max_altitude', 500)
        self.controller.max_speed = self.config.get('flight', {}).get('max_speed', 100)

        # Safety monitoring runs for the whole session (it is idle on the
        # ground), so a lost link can't switch it off mid-flight.
        self.safety = SafetyManager(self.controller, self.config)
        self.safety.set_safety_alert_callback(
            lambda event: self.root.after(0, self._on_safety_event, event))
        self.safety.start_monitoring()

        self.preflight = PreflightChecker(self.controller, self.connection, self.safety, self.config)

    def toggle_video_window(self):
        """Toggle video window open/closed"""
        if self.video_window is None or not self.video_window.is_window_open():
            self.open_video_window()
        else:
            self.video_window.close_video_window()
            self.video_window = None

    def open_video_window(self):
        """Open video window"""
        try:
            self.video_window = VideoWindow(self.root, self.config)
            self.video_window.create_video_window()
            self.log_message("Video window opened")
        except Exception as e:
            self.log_message(f"Failed to open video window: {e}")
            self.logger.error(f"Video window error: {e}")

    def start_video_with_hud(self):
        """Start video streaming with HUD overlay"""
        try:
            # Open video window if not already open
            if self.video_window is None or not self.video_window.is_window_open():
                self.open_video_window()

            # Wait a moment for window to initialize
            if self.video_window:
                # Start video stream automatically
                self.root.after(500, self._auto_start_video)
                self.log_message("Starting video with HUD...")

        except Exception as e:
            self.log_message(f"Failed to start video: {e}")
            self.logger.error(f"Video start error: {e}")

    def _auto_start_video(self):
        """Automatically start video stream in video window"""
        if self.video_window and self.video_window.is_window_open():
            try:
                self.video_window.start_video_stream()
                self.video_status_var.set("Video: Starting...")
                # Update status after a delay
                self.root.after(2000, self._update_video_status)
            except Exception as e:
                self.log_message(f"Auto-start video failed: {e}")
                self.logger.error(f"Auto-start error: {e}")
                self.video_status_var.set("Video: Failed")

    def _update_video_status(self):
        """Update video status indicator"""
        if self.video_window and self.video_window.is_window_open():
            if hasattr(self.video_window.video_stream, 'is_streaming') and self.video_window.video_stream.is_streaming:
                self.video_status_var.set("Video: Active with HUD")
            else:
                self.video_status_var.set("Video: Stopped")
        else:
            self.video_status_var.set("Video: Stopped")

    def connect_drone(self):
        """Connect to drone"""
        def connect_thread():
            try:
                # Update connection parameters
                self.connection.drone_ip = self.ip_var.get()
                self.connection.command_port = int(self.port_var.get())

                self.log_message("Connecting to drone...")

                if self.connection.connect():
                    if self.controller.initialize():
                        self.log_message("Connected and initialized successfully")
                    else:
                        self.log_message("Connected but initialization failed")
                else:
                    self.log_message("Failed to connect to drone")

            except Exception as e:
                self.log_message(f"Connection error: {e}")

        threading.Thread(target=connect_thread, daemon=True).start()

    def disconnect_drone(self):
        """Disconnect from drone - stops logging but preserves safety monitoring."""
        self._stop_logging_on_disconnect()
        self.log_message("Disconnected from drone")

    def on_connection_changed(self, connected: bool):
        """Handle connection status change"""
        self.is_connected = connected

        # Update GUI elements in main thread
        self.root.after(0, self._update_connection_ui, connected)

    def _update_connection_ui(self, connected: bool):
        """Update UI elements based on connection status"""
        if connected:
            self.connection_status_var.set("Connected")
            self.connect_btn.config(state='disabled')
            self.disconnect_btn.config(state='normal')

            # Enable control buttons - except Takeoff, which the pre-flight
            # checklist unlocks once every check passes.
            for name, btn in self.control_buttons.items():
                btn.config(state='disabled' if name == 'takeoff' else 'normal')

        else:
            self.connection_status_var.set("Disconnected")
            self.connect_btn.config(state='normal')
            self.disconnect_btn.config(state='disabled')

            # Disable control buttons
            for btn in self.control_buttons.values():
                btn.config(state='disabled')

    def on_telemetry_received(self, telemetry_data: str):
        """Handle received telemetry data"""
        self.root.after(0, self._update_telemetry_display, telemetry_data)

    def _update_telemetry_display(self, telemetry_data: str):
        """Update telemetry display in GUI"""
        now = time.time()
        self.last_telemetry_update = now
        self._telemetry_times.append(now)

        parsed_data = self.telemetry_parser.parse(telemetry_data)
        if parsed_data:
            # The controller's battery/altitude drive its safety checks
            # (e.g. refusing takeoff on low battery), so keep them live.
            if self.controller:
                self.controller.update_from_telemetry(parsed_data)
            if self.safety:
                self.safety.on_telemetry_update(parsed_data)
            if self.preflight:
                self.preflight.record_telemetry()

            self.status_vars['battery'].set(f"{parsed_data.get('battery', '--')}%")
            self.status_vars['altitude'].set(f"{parsed_data.get('altitude', '--')} cm")
            speed = parsed_data.get('speed_total')
            self.status_vars['speed'].set(f"{speed:.0f} cm/s" if speed is not None else "--")

            # Update video window telemetry if open
            if self.video_window and self.video_window.is_window_open():
                self.video_window.update_telemetry(parsed_data)
                if self.controller:
                    self.video_window.update_flight_status(self.controller.get_flight_status())

        # Telemetry arrives ~10x/s; a readable summary twice a second is plenty.
        if now - self._last_telemetry_text >= 0.5:
            self._last_telemetry_text = now
            line = self.telemetry_parser.format_for_display(parsed_data) if parsed_data else telemetry_data
            current_time = time.strftime("%H:%M:%S")
            self.telemetry_text.insert(tk.END, f"[{current_time}] {line}\n")
            self.telemetry_text.see(tk.END)

            # Limit text length
            if int(self.telemetry_text.index('end-1c').split('.')[0]) > 100:
                self.telemetry_text.delete('1.0', '50.0')

    def log_message(self, message: str):
        """Add message to log display"""
        current_time = time.strftime("%H:%M:%S")
        self.root.after(0, self._add_log_message, f"[{current_time}] {message}")

    def _add_log_message(self, message: str):
        """Add message to log text area"""
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)

        # Limit log length
        if int(self.log_text.index('end-1c').split('.')[0]) > 100:
            self.log_text.delete('1.0', '50.0')

    def takeoff_drone(self):
        """
        Initiate drone takeoff, gated by a fresh pre-flight check.

        Every path (button, 't' key) comes through here. The check runs again
        with a live command probe, because the panel's last passive check may
        be up to half a second old and can't prove the aircraft hears us.
        """
        if not self.controller or self._takeoff_pending:
            return
        self._takeoff_pending = True
        self.control_buttons['takeoff'].config(state='disabled')
        self.log_message("Running pre-flight checks...")

        def check_thread():
            report = self.preflight.run(probe=True) if self.preflight else None
            self.root.after(0, self._takeoff_after_check, report)

        threading.Thread(target=check_thread, daemon=True).start()

    def _takeoff_after_check(self, report):
        """UI thread: act on the pre-flight result."""
        if report is not None:
            self._show_preflight(report)
            if report.failures:
                self._takeoff_pending = False
                self.log_message("TAKEOFF BLOCKED: " + "; ".join(
                    f"{r.name}: {r.detail}" for r in report.failures))
                return
            if report.warnings:
                lines = "\n".join(f"- {r.name}: {r.detail}" for r in report.warnings)
                if not messagebox.askyesno("Pre-flight warnings",
                                           f"Take off with these warnings?\n\n{lines}"):
                    self._takeoff_pending = False
                    self.log_message("Takeoff cancelled at pre-flight warnings")
                    return
                self.log_message("Pre-flight warnings acknowledged: " + "; ".join(
                    r.name for r in report.warnings))

        def takeoff_thread():
            try:
                if self.controller.takeoff():
                    self.log_message("Takeoff successful")
                    self.root.after(0, lambda: self.status_vars['flying'].set("Yes"))
                else:
                    self.log_message("Takeoff failed")
            finally:
                self._takeoff_pending = False

        threading.Thread(target=takeoff_thread, daemon=True).start()

    def land_drone(self):
        """Initiate drone landing"""
        if self.controller:
            def land_thread():
                if self.controller.land():
                    self.log_message("Landing successful")
                    self.root.after(0, lambda: self.status_vars['flying'].set("No"))
                else:
                    self.log_message("Landing failed")

            threading.Thread(target=land_thread, daemon=True).start()

    def move_drone(self, direction: str):
        """Move drone in specified direction"""
        if not self.controller:
            return

        distance = 30  # Default movement distance in cm

        def move_thread():
            success = False
            if direction == 'forward':
                success = self.controller.move_forward(distance)
            elif direction == 'backward':
                success = self.controller.move_backward(distance)
            elif direction == 'left':
                success = self.controller.move_left(distance)
            elif direction == 'right':
                success = self.controller.move_right(distance)
            elif direction == 'up':
                success = self.controller.move_up(distance)
            elif direction == 'down':
                success = self.controller.move_down(distance)

            if success:
                self.log_message(f"Moved {direction} {distance}cm")
            else:
                self.log_message(f"Failed to move {direction}")

        threading.Thread(target=move_thread, daemon=True).start()

    def rotate_drone(self, direction: str):
        """Rotate drone in specified direction"""
        if not self.controller:
            return

        degrees = 45  # Default rotation in degrees

        def rotate_thread():
            success = False
            if direction == 'left':
                success = self.controller.rotate_counterclockwise(degrees)
            elif direction == 'right':
                success = self.controller.rotate_clockwise(degrees)

            if success:
                self.log_message(f"Rotated {direction} {degrees}°")
            else:
                self.log_message(f"Failed to rotate {direction}")

        threading.Thread(target=rotate_thread, daemon=True).start()

    def update_speed(self, value):
        """Update drone speed setting"""
        speed = int(float(value))
        self.speed_label.config(text=str(speed))

        if self.controller:
            def speed_thread():
                if self.controller.set_speed(speed):
                    self.log_message(f"Speed set to {speed}")
                else:
                    self.log_message("Failed to set speed")

            threading.Thread(target=speed_thread, daemon=True).start()

    def emergency_stop(self):
        """Execute emergency stop (operator only - cuts the motors)"""
        if self.controller:
            if messagebox.askyesno("Emergency Stop", "Are you sure you want to execute emergency stop?"):
                # Off the UI thread: waiting for the acknowledgement must not freeze the window.
                target = (lambda: self.safety.trigger_emergency_stop("Operator emergency stop")) \
                    if self.safety else self.controller.emergency_stop
                threading.Thread(target=target, daemon=True).start()
                self.log_message("EMERGENCY STOP EXECUTED")
                self.status_vars['flying'].set("No")

    SAFETY_COLOURS = {
        SafetyLevel.WARNING: ("#f9a825", "black"),
        SafetyLevel.CRITICAL: ("#c62828", "white"),
        SafetyLevel.EMERGENCY: ("#b71c1c", "white"),
    }

    def _on_safety_event(self, event):
        """Show a safety event (runs on the UI thread)."""
        level, message, kind = event['level'], event['message'], event['type']
        self.log_message(f"SAFETY {level.value.upper()}: {message}")

        if kind in ('takeoff', 'landing'):
            self.safety_label.config(text="Safety: OK", bg="#2e7d32", fg="white")
        elif kind == 'connection_restored':
            self.safety_label.config(text="Safety: link restored", bg="#2e7d32", fg="white")
        elif level in self.SAFETY_COLOURS:
            bg, fg = self.SAFETY_COLOURS[level]
            self.safety_label.config(text=f"{level.value.upper()}: {message}", bg=bg, fg=fg)

    def toggle_sim_link(self):
        """Simulator fault injection: cut or restore the radio link."""
        if not self.sim_drone:
            return
        up = not self.sim_drone.link_up
        self.sim_drone.set_link(up)
        self.link_fault_btn.config(text="Cut link" if up else "Restore link")
        self.log_message(f"SIMULATOR: radio link {'restored' if up else 'cut'}")

    def update_gui(self):
        """Main GUI update loop"""
        try:
            if self.controller:
                status = self.controller.get_flight_status()
                self.status_vars['mode'].set(status['mode'])
                self.status_vars['flying'].set("Yes" if status['is_flying'] else "No")

            self._update_link_status()
            self._refresh_preflight()

        except Exception as e:
            self.logger.error(f"GUI update error: {e}")
        finally:
            # Always reschedule: one bad update must not freeze the display.
            self.root.after(500, self.update_gui)

    def _update_link_status(self):
        """Show telemetry rate while it flows, and its age when it stops."""
        now = time.time()
        self._telemetry_times = [t for t in self._telemetry_times if now - t <= 2.0]
        if not self.is_connected:
            self.status_vars['link'].set("--")
        elif self._telemetry_times:
            self.status_vars['link'].set(f"{len(self._telemetry_times) / 2.0:.0f} Hz")
        elif self.last_telemetry_update:
            self.status_vars['link'].set(f"STALE {now - self.last_telemetry_update:.0f}s")
        else:
            self.status_vars['link'].set("waiting")

    def run(self):
        """Start the GUI application"""
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.logger.info("Application interrupted by user")
        except Exception as e:
            self.logger.error(f"Application error: {e}")
        finally:
            self._app_shutdown()