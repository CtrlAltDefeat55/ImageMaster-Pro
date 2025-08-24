import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog, colorchooser, font
import uuid # For unique overlay IDs

# Import TkinterDnD AFTER other tkinter imports but potentially before ThemedTk is used heavily
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _tkdnd_available = True
except ImportError:
    _tkdnd_available = False
    # Define dummy classes/variables if tkdnd is not available
    class TkinterDnD: # Dummy class
        @staticmethod
        def Tk(*args, **kwargs):
            print("Warning: TkinterDnD not found. Using standard tk.Tk(). Drag and Drop disabled.")
            return tk.Tk(*args, **kwargs)
    DND_FILES = None # Dummy variable

from ttkthemes import ThemedTk # Now used for the main frame, not the root itself
from PIL import Image, ImageFilter, ImageTk, ImageDraw, ImageFont, ExifTags, ImageEnhance, UnidentifiedImageError
import os
import json
import threading
import time
import math
import re # For regex parsing in handle_drop
import sys  # For checking platform
from collections import deque # For Undo/Redo stacks

# --- Tooltip Helper Class ---
class ToolTip:
    """Simple Tooltip class for Tkinter widgets with debouncing"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip = None
        self._enter_job = None
        self._hide_job = None # Added to prevent flickering
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)
        self.widget.bind("<ButtonPress>", self.leave)  # Hide on click

    def enter(self, event=None):
        self._cancel_hide()
        if not self.tooltip: # Only schedule show if not already visible
             if self._enter_job:
                 self.widget.after_cancel(self._enter_job)
             self._enter_job = self.widget.after(500, self._show_tooltip)  # Show after 500ms

    def _show_tooltip(self):
        if self.tooltip:
            return
        try:
            # Get widget position relative to the screen
            x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5 # Position below the widget

            self.tooltip = tk.Toplevel(self.widget)
            self.tooltip.wm_overrideredirect(True) # No window decorations
            self.tooltip.wm_geometry(f"+{x}+{y}")
            # Adjust position if tooltip goes off-screen (simple adjustment)
            self.tooltip.update_idletasks() # Ensure geometry is calculated
            scr_w = self.widget.winfo_screenwidth()
            scr_h = self.widget.winfo_screenheight()
            tip_w = self.tooltip.winfo_width()
            tip_h = self.tooltip.winfo_height()

            if x + tip_w > scr_w:
                 x = scr_w - tip_w - 5
            if x < 0 : x = 5 # Prevent going off left edge

            if y + tip_h > scr_h:
                 y = self.widget.winfo_rooty() - tip_h - 5 # Position above
            if y < 0 : y = 5 # Prevent going off top edge

            self.tooltip.wm_geometry(f"+{x}+{y}")


            label = tk.Label(self.tooltip, text=self.text, justify='left',
                             background="#ffffe0", relief='solid', borderwidth=1,
                             wraplength=300, font=("tahoma", "8", "normal"))
            label.pack(ipadx=1)
            # Add bindings to the tooltip itself to hide it
            label.bind("<Leave>", self.leave)
            label.bind("<ButtonPress>", self.leave)
        except Exception as e:
            print(f"Error showing tooltip: {e}")
            self._destroy_tooltip()

    def leave(self, event=None):
        self._cancel_show()
        # Don't hide immediately, wait a bit in case the cursor moves onto the tooltip
        if self.tooltip and not self._hide_job:
            self._hide_job = self.widget.after(100, self._check_hide)

    def _check_hide(self):
        # Check if cursor is over the widget or the tooltip
        if self.tooltip:
            try:
                widget_x, widget_y = self.widget.winfo_rootx(), self.widget.winfo_rooty()
                widget_w, widget_h = self.widget.winfo_width(), self.widget.winfo_height()
                tt_x, tt_y = self.tooltip.winfo_rootx(), self.tooltip.winfo_rooty()
                tt_w, tt_h = self.tooltip.winfo_width(), self.tooltip.winfo_height()
                cursor_x, cursor_y = self.widget.winfo_pointerxy()

                over_widget = (widget_x <= cursor_x < widget_x + widget_w and
                               widget_y <= cursor_y < widget_y + widget_h)
                over_tooltip = (tt_x <= cursor_x < tt_x + tt_w and
                                tt_y <= cursor_y < tt_y + tt_h)

                if not over_widget and not over_tooltip:
                    self._destroy_tooltip()
            except (tk.TclError, AttributeError): # Handle cases where widget/tooltip might be gone
                self._destroy_tooltip()

        self._hide_job = None


    def _cancel_show(self):
        if self._enter_job:
            self.widget.after_cancel(self._enter_job)
            self._enter_job = None

    def _cancel_hide(self):
        if self._hide_job:
            self.widget.after_cancel(self._hide_job)
            self._hide_job = None

    def _destroy_tooltip(self):
        self._cancel_show()
        if self.tooltip:
            try:
                self.tooltip.destroy()
            except tk.TclError: # Handle case where window is already destroyed
                pass
            self.tooltip = None


# --- Main Application Class ---
class ImageMasterProApp:
    # --- Constants ---
    MAX_UNDO_HISTORY = 50 # Limit undo history size

    def __init__(self, root):
        # root is now the TkinterDnD enabled tk.Tk() instance (if available)
        self.root = root

        # Make root window resizable
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Apply themed style using a main ThemedTk Frame inside the root
        self.themed_style = ttk.Style()
        try:
            # Get available themes using the style object associated with the root window
            available_themes = ThemedTk(self.root, theme="arc").get_themes() # Temporary instance to get themes
        except Exception as e:
            print(f"Warning: Could not get themes using ThemedTk: {e}")
            available_themes = self.themed_style.theme_names() # Fallback to standard ttk themes
            if not available_themes: available_themes = ["clam", "alt", "default", "classic"] # Further fallback
            print(f"Falling back to ttk themes: {available_themes}")

        # --- State Variables ---
        self.image_list = []
        self.current_image_path = None
        self.original_image = None # Holds the PIL Image object (never directly modified by effects after initial load+EXIF)
        self.rotated_flipped_image = None # Holds original_image after rotation/flip applied (base for processing)
        self.processed_image = None # Holds the PIL image after *all* processing FOR PREVIEW (incl. overlays)
        self.preview_image_tk = None # Holds the ImageTk object for canvas
        self.output_dir = tk.StringVar(value="")
        self.filename_var = tk.StringVar()
        self.theme_var = tk.StringVar(value="arc") # Default theme
        self.processed_base_size = None # Stores (width, height) of image after filter/resize/adjustments, before edits/watermark/overlays
        self.image_settings = {} # Stores per-image settings {image_path: {'blur_areas': [], 'adjustments': {}, 'overlays': [], 'undo_stack': deque(), 'redo_stack': deque(), ...}}

        # --- Drawing/Selection state ---
        self.selection_rect_id = None
        self.selection_start_coords = None # Canvas coords
        self.selection_current_coords = None # Canvas coords
        self.current_selection_original = None # Stores (shape, coords_orig, [strength]) for the *current* drag operation (coords in *original* image space)
        self.edit_shape = tk.StringVar(value="rectangle") # 'rectangle' or 'circle'
        self.blur_strength = tk.IntVar(value=50) # Default blur radius

        # --- State for editing existing blur/blackout/overlay areas ---
        self.selected_area_uuid = None # UUID of the selected blur, blackout, or overlay area
        self.selected_area_type = None # 'blur', 'blackout', or 'overlay'
        self.edit_interaction_mode = None # 'drag' (initially, maybe 'resize' later) or 'rotate', 'resize_tl' etc. for overlays
        self.edit_drag_start_coords = None # Original (x0, y0) of the area when drag starts (in *original* image coords)
        self.edit_drag_mouse_start = None # Canvas (x, y) where mouse drag started
        self.edit_orig_rect_on_drag_start = None # Original rect of area on drag start (original coords)
        self.edit_orig_angle_on_drag_start = 0.0 # Original angle on drag start
        self.edit_center_on_drag_start = (0,0) # Center on drag start (original coords)
        self.edit_mouse_start_angle_on_drag = 0.0 # Relative mouse angle for rotation start

        # --- Watermark state (Text) ---
        self.watermark_text = tk.StringVar(value="SAMPLE")
        self.watermark_font_size = tk.IntVar(value=40) # Adjusted default
        self.watermark_color = tk.StringVar(value="#FFFFFF")
        self.watermark_opacity = tk.IntVar(value=128) # Adjusted default
        self.watermark_position = tk.StringVar(value="Diagonal Fit")
        self.use_text_watermark = tk.BooleanVar(value=False)

        # --- Image Watermark State (Manual Placement Mode) ---
        # Note: This now shares interaction logic with overlays. We select *either* the WM *or* an overlay.
        self.use_image_watermark = tk.BooleanVar(value=False)
        self.watermark_image_path = tk.StringVar(value="")
        self.watermark_image_opacity = tk.IntVar(value=128)
        self.watermark_image_position = tk.StringVar(value="Manual Placement")
        # wm_img_info stores the state of the single *main* image watermark when in manual mode.
        # This is now also stored within image_settings per image.
        self.wm_img_info = { # Default structure, loaded per image
            'path': None,
            'pil_image': None, # Loaded PIL image for the watermark
            'rect': None, # Bounding box [x0, y0, x1, y1] ON THE ORIGINAL (unresized) main image coords
            'angle': 0.0, # Rotation angle in degrees
            'opacity': 128 # Opacity (needed per-instance if generalized)
        }
        # Interaction variables (self.edit_interaction_mode etc. are now used)

        # --- Multiple Image Overlays State ---
        self.selected_overlay_uuid = None # UUID of the currently selected overlay in the listbox
        # Overlay data itself is stored in image_settings[current_image_path]['overlays']
        # Each overlay: {'uuid': str, 'path': str, 'pil_image': Image, 'rect': tuple, 'angle': float, 'opacity': int}

        # --- Zoom/Pan State ---
        self.zoom_factor = 1.0
        self.pan_offset = [0, 0] # Canvas pixels [dx, dy] from top-left (0,0)
        self._pan_start_x = 0
        self._pan_start_y = 0
        self._pan_active = False

        # --- Adjustments State ---
        self.brightness_var = tk.DoubleVar(value=1.0)
        self.contrast_var = tk.DoubleVar(value=1.0)
        self.saturation_var = tk.DoubleVar(value=1.0)

        # --- Undo/Redo State ---
        # Stacks are stored per image in self.image_settings[path]['undo_stack'/'redo_stack']
        # We need references to the current image's stacks
        self.current_undo_stack = deque(maxlen=self.MAX_UNDO_HISTORY)
        self.current_redo_stack = deque()

        # --- Other State ---
        self.available_themes = available_themes # Store available themes
        self._preview_update_job = None # For debouncing preview updates
        self._canvas_resize_job = None # For debouncing canvas resize

        # --- Initialize UI and Settings ---
        self.init_style() # Apply initial theme
        self.init_ui()
        self.load_presets() # Load saved global settings
        self.update_widget_states()
        self.update_undo_redo_buttons() # Initial state
        self.root.geometry("1600x1300") # Set initial size (increased)

        # Store original widget backgrounds for hover effects
        self._original_widget_bgs = {}


    def init_style(self):
        """Initializes styles and applies the initial theme."""
        try:
            # Ensure saved theme exists
            saved_theme = self.theme_var.get() # Get potentially loaded theme
            if saved_theme not in self.available_themes:
                print(f"Warning: Saved theme '{saved_theme}' not found. Available: {self.available_themes}")
                self.theme_var.set(self.available_themes[0] if self.available_themes else "clam")

            # Set theme using the style object
            self.themed_style.theme_use(self.theme_var.get())
            print(f"Applied theme: {self.theme_var.get()}")
            # Configure styles that might be theme-dependent
            self.themed_style.configure("DND.TLabel", padding=5)
            self.themed_style.configure("Zoom.TLabel", padding=(0, 5), font=("Segoe UI", 8)) # Style for zoom label
            self.themed_style.configure("Header.TLabel", font=("Segoe UI", 9, "bold"))

        except Exception as e:
            messagebox.showerror("Style Error", f"Failed to initialize styles or theme '{self.theme_var.get()}': {e}")
            try:
                # Fallback to a known default ttk theme
                self.themed_style.theme_use("clam")
                self.theme_var.set("clam")
                print("Applied fallback theme: clam")
            except Exception as fallback_e:
                print(f"Fatal: Could not apply fallback theme 'clam'. Error: {fallback_e}")

    def init_ui(self):
        """Builds the user interface widgets."""
        self.root.title("ImageMaster Pro Enhanced")

        # Main frame using ttk for theming
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(1, weight=1) # Preview area takes extra space
        main_frame.rowconfigure(0, weight=1) # Preview area takes extra space

        # --- Left Controls Panel ---
        controls_frame = ttk.Frame(main_frame, padding="10")
        controls_frame.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        # Allow different sections to expand if needed, give processing notebook more weight
        controls_frame.rowconfigure(0, weight=0) # File I/O
        controls_frame.rowconfigure(1, weight=0) # Actions
        controls_frame.rowconfigure(2, weight=1) # Processing Notebook (give weight)

        # --- File Handling Frame ---
        file_frame = ttk.LabelFrame(controls_frame, text="File Input/Output", padding="10")
        file_frame.grid(row=0, column=0, sticky="new", pady=5) # Grid instead of pack

        self.drag_drop_frame = ttk.Frame(file_frame, relief="sunken", borderwidth=1, width=300, height=80) # Adjusted size
        self.drag_drop_frame.pack(fill="x", pady=(0, 10))
        self.drag_drop_frame.pack_propagate(False)
        self.dnd_label = ttk.Label(self.drag_drop_frame,
                                 text="Drop images or folders here" if _tkdnd_available else "Drag & Drop (Disabled)",
                                 anchor=tk.CENTER, style="DND.TLabel")
        self.dnd_label.pack(expand=True, fill="both", padx=10, pady=10)

        if _tkdnd_available:
            try:
                self.drag_drop_frame.drop_target_register(DND_FILES)
                self.drag_drop_frame.dnd_bind('<<Drop>>', self.handle_drop)
                self.dnd_label.drop_target_register(DND_FILES)
                self.dnd_label.dnd_bind('<<Drop>>', self.handle_drop)
                ToolTip(self.drag_drop_frame, "Drop image files or folders containing images here.")
            except Exception as e:
                print(f"Error registering drop target: {e}")
                self.dnd_label.config(text="Drag & Drop (Error!)")
                ToolTip(self.drag_drop_frame, "Drag and drop failed to initialize.")
        else:
            ToolTip(self.drag_drop_frame, "Drag and drop is disabled. Ensure TkDnD is installed. Use 'Browse'.")

        # Add hover effect for DND area (only if tkdnd is available)
        if _tkdnd_available:
            self.drag_drop_frame.bind("<Enter>", self.on_dnd_enter)
            self.drag_drop_frame.bind("<Leave>", self.on_dnd_leave)
            self.dnd_label.bind("<Enter>", self.on_dnd_enter)
            self.dnd_label.bind("<Leave>", self.on_dnd_leave)


        # --- FIX: Assign browse_button to self.browse_button ---
        self.browse_button = ttk.Button(file_frame, text="Browse Files", command=self.browse_files) # Assign to self.
        self.browse_button.pack(fill="x", pady=5)
        ToolTip(self.browse_button, "Select image files to process (replaces current list).") # Use self. here too
        # --- END FIX ---

        self.filename_label = ttk.Label(file_frame, text="Output Filename:")
        self.filename_label.pack(fill="x", pady=(5, 0))
        self.filename_entry = ttk.Entry(file_frame, textvariable=self.filename_var)
        self.filename_entry.pack(fill="x", pady=(0, 5))
        ToolTip(self.filename_entry, "Base name for output file (extension added automatically). Use <#> for sequence.")

        output_dir_frame = ttk.Frame(file_frame)
        output_dir_frame.pack(fill="x", pady=5)
        self.output_dir_button = ttk.Button(output_dir_frame, text="Output Directory", command=self.select_output_dir)
        self.output_dir_button.pack(side=tk.LEFT, padx=(0, 5)) # Don't expand button
        ToolTip(self.output_dir_button, "Select folder for processed images. Empty saves next to original.")
        self.output_dir_label = ttk.Label(output_dir_frame, textvariable=self.output_dir, relief="sunken", anchor=tk.W, width=20)
        self.output_dir_label.pack(side=tk.LEFT, expand=True, fill="x")
        ToolTip(self.output_dir_label, "Current output directory.")

        # --- Action Frame ---
        action_frame = ttk.LabelFrame(controls_frame, text="Actions", padding="10")
        action_frame.grid(row=1, column=0, sticky="new", pady=5) # Grid instead of pack
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)
        action_frame.columnconfigure(2, weight=1) # For undo/redo

        # Row 0: Convert Buttons
        self.convert_one_button = ttk.Button(action_frame, text="Convert Current", command=lambda: self.confirm_conversion(single=True))
        self.convert_one_button.grid(row=0, column=0, padx=2, pady=5, sticky="ew")
        ToolTip(self.convert_one_button, "Process ONLY the currently selected image with current settings.")

        self.convert_all_button = ttk.Button(action_frame, text="Convert All", command=lambda: self.confirm_conversion(single=False))
        self.convert_all_button.grid(row=0, column=1, columnspan=2, padx=2, pady=5, sticky="ew") # Span 2 columns
        ToolTip(self.convert_all_button, "Process ALL loaded images with global settings and per-image edits/overlays.")

        # Row 1: Undo/Redo
        self.undo_button = ttk.Button(action_frame, text="Undo", command=self.undo, state=tk.DISABLED)
        self.undo_button.grid(row=1, column=0, padx=2, pady=5, sticky="ew")
        ToolTip(self.undo_button, "Undo the last action for the current image (Ctrl+Z).")
        self.redo_button = ttk.Button(action_frame, text="Redo", command=self.redo, state=tk.DISABLED)
        self.redo_button.grid(row=1, column=1, padx=2, pady=5, sticky="ew")
        ToolTip(self.redo_button, "Redo the last undone action for the current image (Ctrl+Y).")

        # Row 2: Reset/Save
        self.reset_button = ttk.Button(action_frame, text="Reset All", command=self.reset_all)
        self.reset_button.grid(row=2, column=0, padx=2, pady=5, sticky="ew")
        ToolTip(self.reset_button, "Reset global settings, clear all images and edits.")

        self.save_preset_button = ttk.Button(action_frame, text="Save Settings", command=self.save_presets)
        self.save_preset_button.grid(row=2, column=1, padx=2, pady=5, sticky="ew")
        ToolTip(self.save_preset_button, "Save current global settings (format, filters, watermarks, theme, etc.).")

        # Row 3: Theme Selection
        theme_frame = ttk.Frame(action_frame)
        theme_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0)) # Span 3 cols
        ttk.Label(theme_frame, text="Theme:").pack(side=tk.LEFT, padx=5)
        theme_state = "readonly" if self.available_themes else "disabled"
        if not self.available_themes:
            self.theme_var.set("clam") # Force default if none found

        self.theme_menu = ttk.Combobox(theme_frame, textvariable=self.theme_var, state=theme_state, values=self.available_themes)
        self.theme_menu.pack(side=tk.LEFT, expand=True, fill="x")
        if theme_state != "disabled":
            self.theme_menu.bind("<<ComboboxSelected>>", self.change_theme_action)
        ToolTip(self.theme_menu, "Change application’s visual theme.")

        # --- Processing Notebook (Tabs for different settings) ---
        self.processing_notebook = ttk.Notebook(controls_frame)
        self.processing_notebook.grid(row=2, column=0, sticky="nsew", pady=10) # Grid instead of pack

        # --- Tab 1: General Settings ---
        general_settings_frame = ttk.Frame(self.processing_notebook, padding="10")
        self.processing_notebook.add(general_settings_frame, text=" General ")
        general_settings_frame.columnconfigure(1, weight=1) # Allow comboboxes/entries to expand

        # Presets
        preset_label = ttk.Label(general_settings_frame, text="Preset:")
        preset_label.grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.preset_var = tk.StringVar(value="Custom")
        preset_values = ["Custom", "YouTube Thumbnail (1280x720)", "Facebook Post (1200x630)",
                         "Instagram Post (1080x1080)", "Twitter Post (1024x512)"]
        self.preset_menu = ttk.Combobox(general_settings_frame, textvariable=self.preset_var, state="readonly", values=preset_values)
        self.preset_menu.grid(row=0, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        self.preset_menu.bind("<<ComboboxSelected>>", self.apply_preset_action)
        ToolTip(self.preset_menu, "Apply predefined size settings (overrides manual size).")

        # Format
        format_label = ttk.Label(general_settings_frame, text="Format:")
        format_label.grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.format_var = tk.StringVar(value="PNG")
        self.format_menu = ttk.Combobox(general_settings_frame, textvariable=self.format_var, state="readonly",
                                        values=["PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP"])
        self.format_menu.grid(row=1, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        self.format_menu.bind("<<ComboboxSelected>>", self.on_format_change)
        ToolTip(self.format_menu, "Select output image format.")

        # Quality (for JPEG/WEBP)
        self.quality_label = ttk.Label(general_settings_frame, text="Quality:") # Generic name
        self.quality_label.grid(row=2, column=0, padx=5, pady=5, sticky="w")
        vcmd_quality = (self.root.register(self.validate_quality), '%P')
        self.quality_var = tk.StringVar(value="95")
        self.quality_entry = ttk.Entry(general_settings_frame, textvariable=self.quality_var, width=5, validate='key', validatecommand=vcmd_quality)
        self.quality_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w") # Only col 1
        ToolTip(self.quality_entry, "Quality for JPEG/WEBP (1-100). Higher = better quality, larger size.")

        # Resize (Width/Height)
        vcmd_dim = (self.root.register(self.validate_dimension), '%P')
        resize_label = ttk.Label(general_settings_frame, text="Resize (WxH):")
        resize_label.grid(row=3, column=0, padx=5, pady=5, sticky="w")
        self.resize_width_var = tk.StringVar()
        self.resize_height_var = tk.StringVar()
        self.resize_width_entry = ttk.Entry(general_settings_frame, width=7, textvariable=self.resize_width_var, validate='key', validatecommand=vcmd_dim)
        self.resize_width_entry.grid(row=3, column=1, padx=(5, 2), pady=5, sticky="w")
        self.resize_height_entry = ttk.Entry(general_settings_frame, width=7, textvariable=self.resize_height_var, validate='key', validatecommand=vcmd_dim)
        self.resize_height_entry.grid(row=3, column=2, padx=(2, 5), pady=5, sticky="w")
        self.resize_width_entry.bind("<FocusOut>", self.update_preview_debounced) # Use debounced
        self.resize_height_entry.bind("<FocusOut>", self.update_preview_debounced) # Use debounced
        ToolTip(self.resize_width_entry, "Output width in pixels.")
        ToolTip(self.resize_height_entry, "Output height in pixels.")
        self.resize_info_label = ttk.Label(general_settings_frame, text="(Leave blank to maintain aspect ratio or current size)")
        self.resize_info_label.grid(row=4, column=0, columnspan=4, padx=5, pady=(0, 5), sticky="w")

        # Filter
        filter_label = ttk.Label(general_settings_frame, text="Filter:")
        filter_label.grid(row=5, column=0, padx=5, pady=5, sticky="w")
        self.filter_var = tk.StringVar(value="None")
        self.filter_menu = ttk.Combobox(general_settings_frame, textvariable=self.filter_var, state="readonly",
                                        values=["None", "Grayscale", "Sepia", "Blur", "Sharpen", "Edge Enhance", "Contour"])
        self.filter_menu.grid(row=5, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        self.filter_menu.bind("<<ComboboxSelected>>", self.apply_filter_action) # Use action for undo
        ToolTip(self.filter_menu, "Apply a filter effect to the image.")

        # Basic Transforms (Rotation/Flip)
        transform_frame = ttk.Frame(general_settings_frame)
        transform_frame.grid(row=6, column=0, columnspan=4, pady=5, sticky="ew")
        self.rotate_ccw_button = ttk.Button(transform_frame, text="Rotate L", command=lambda: self.apply_transform_action('rotate', 90))
        self.rotate_ccw_button.pack(side=tk.LEFT, padx=2)
        ToolTip(self.rotate_ccw_button, "Rotate 90° counter-clockwise (applied to original).")
        self.rotate_cw_button = ttk.Button(transform_frame, text="Rotate R", command=lambda: self.apply_transform_action('rotate', -90))
        self.rotate_cw_button.pack(side=tk.LEFT, padx=2)
        ToolTip(self.rotate_cw_button, "Rotate 90° clockwise (applied to original).")
        self.flip_h_button = ttk.Button(transform_frame, text="Flip H", command=lambda: self.apply_transform_action('flip', "H"))
        self.flip_h_button.pack(side=tk.LEFT, padx=2)
        ToolTip(self.flip_h_button, "Flip horizontally (applied to original).")
        self.flip_v_button = ttk.Button(transform_frame, text="Flip V", command=lambda: self.apply_transform_action('flip', "V"))
        self.flip_v_button.pack(side=tk.LEFT, padx=2)
        ToolTip(self.flip_v_button, "Flip vertically (applied to original).")

        # --- Tab 2: Adjustments ---
        adjustments_frame = ttk.Frame(self.processing_notebook, padding="10")
        self.processing_notebook.add(adjustments_frame, text=" Adjust ")
        adjustments_frame.columnconfigure(1, weight=1)

        ttk.Label(adjustments_frame, text="Brightness:", anchor="w").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        brightness_scale = ttk.Scale(adjustments_frame, from_=0.1, to=3.0, variable=self.brightness_var, orient=tk.HORIZONTAL, command=self.update_preview_debounced)
        brightness_scale.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        brightness_scale.bind("<ButtonRelease-1>", self.record_adjustment_change) # Record for undo on release
        ToolTip(brightness_scale, "Adjust brightness (1.0 = original).")

        ttk.Label(adjustments_frame, text="Contrast:", anchor="w").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        contrast_scale = ttk.Scale(adjustments_frame, from_=0.1, to=3.0, variable=self.contrast_var, orient=tk.HORIZONTAL, command=self.update_preview_debounced)
        contrast_scale.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        contrast_scale.bind("<ButtonRelease-1>", self.record_adjustment_change)
        ToolTip(contrast_scale, "Adjust contrast (1.0 = original).")

        ttk.Label(adjustments_frame, text="Saturation:", anchor="w").grid(row=2, column=0, padx=5, pady=5, sticky="w")
        saturation_scale = ttk.Scale(adjustments_frame, from_=0.0, to=3.0, variable=self.saturation_var, orient=tk.HORIZONTAL, command=self.update_preview_debounced)
        saturation_scale.grid(row=2, column=1, padx=5, pady=5, sticky="ew")
        saturation_scale.bind("<ButtonRelease-1>", self.record_adjustment_change)
        ToolTip(saturation_scale, "Adjust color saturation (0=grayscale, 1.0=original).")

        reset_adjust_button = ttk.Button(adjustments_frame, text="Reset Adjustments", command=self.reset_adjustments_action)
        reset_adjust_button.grid(row=3, column=0, columnspan=2, padx=5, pady=10, sticky="ew")
        ToolTip(reset_adjust_button, "Reset Brightness, Contrast, and Saturation to original (1.0).")

        # --- Tab 3: Manual Edits (Blur/Blackout) ---
        manual_edit_frame = ttk.Frame(self.processing_notebook, padding="10")
        self.processing_notebook.add(manual_edit_frame, text=" Edits ")
        manual_edit_frame.columnconfigure(1, weight=1)

        ttk.Label(manual_edit_frame, text="Draw on Preview:", style="Header.TLabel").pack(fill="x", pady=(0,5))

        # Shape Selection
        shape_frame = ttk.Frame(manual_edit_frame)
        shape_frame.pack(fill="x", pady=(0,5))
        ttk.Label(shape_frame, text="Shape:").pack(side=tk.LEFT, padx=(0,5))
        ttk.Radiobutton(shape_frame, text="Rect", variable=self.edit_shape, value="rectangle").pack(side=tk.LEFT)
        ttk.Radiobutton(shape_frame, text="Circle", variable=self.edit_shape, value="circle").pack(side=tk.LEFT)
        ToolTip(shape_frame, "Select shape for blur/blackout areas.")

        # Blur Settings
        blur_frame = ttk.Frame(manual_edit_frame)
        blur_frame.pack(fill="x", pady=2)
        self.blur_area_button = ttk.Button(blur_frame, text="Add Blur Area", command=lambda: self.add_edit_area_action(blur=True)) # Use action
        self.blur_area_button.pack(side=tk.LEFT, padx=(0, 5))
        ToolTip(self.blur_area_button, "Add the selected area (Rect/Circle) on preview to the list of areas to be blurred.")
        ttk.Label(blur_frame, text="Strength:").pack(side=tk.LEFT, padx=(5,0))
        blur_scale = ttk.Scale(blur_frame, from_=1, to=100, variable=self.blur_strength, orient=tk.HORIZONTAL) # Increased max range
        # Don't update preview on slide, only when adding
        blur_scale.pack(side=tk.LEFT, fill="x", expand=True, padx=5)
        ToolTip(blur_scale, "Blur intensity (radius) for *new* blur areas.")

        # Blackout Settings
        blackout_frame = ttk.Frame(manual_edit_frame)
        blackout_frame.pack(fill="x", pady=2)
        self.blackout_area_button = ttk.Button(blackout_frame, text="Add Blackout Area", command=lambda: self.add_edit_area_action(blur=False)) # Use action
        self.blackout_area_button.pack(side=tk.LEFT, padx=(0, 5))
        ToolTip(self.blackout_area_button, "Add the selected area (Rect/Circle) on preview to the list of areas to be blacked out.")

        # Edit Management
        ttk.Label(manual_edit_frame, text="Manage Existing Edits:", style="Header.TLabel").pack(fill="x", pady=(10,5))
        self.edit_remove_button = ttk.Button(manual_edit_frame, text="Remove Selected Edit", command=self.remove_selected_area_action, state=tk.DISABLED)
        self.edit_remove_button.pack(fill="x", pady=(0,5))
        ToolTip(self.edit_remove_button, "Remove the currently selected (yellow outline) blur or blackout area.")

        self.clear_areas_button = ttk.Button(manual_edit_frame, text="Clear All Blur/Blackout Areas", command=self.clear_manual_areas_action) # Use action
        self.clear_areas_button.pack(fill="x", pady=(0, 5))
        ToolTip(self.clear_areas_button, "Remove all blur and blackout areas for the current image.")

        # --- Tab 4: Watermarks ---
        watermark_frame = ttk.Frame(self.processing_notebook, padding="10")
        self.processing_notebook.add(watermark_frame, text=" Watermarks ")
        watermark_frame.columnconfigure(1, weight=1)

        # --- Text Watermark Sub-Frame ---
        text_wm_subframe = ttk.LabelFrame(watermark_frame, text="Text Watermark (Global)", padding=10)
        text_wm_subframe.pack(fill="x", pady=(0, 10))
        text_wm_subframe.columnconfigure(1, weight=1)

        ttk.Checkbutton(text_wm_subframe, text="Enable Text Watermark", variable=self.use_text_watermark,
                        command=self.record_text_wm_change).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(text_wm_subframe, text="Text:").grid(row=1, column=0, sticky="w", padx=2, pady=2)
        wm_text_entry = ttk.Entry(text_wm_subframe, textvariable=self.watermark_text)
        wm_text_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=2, pady=2)
        wm_text_entry.bind("<FocusOut>", self.record_text_wm_change) # Record for undo
        ToolTip(wm_text_entry, "Text for the watermark.")

        ttk.Label(text_wm_subframe, text="Size:").grid(row=2, column=0, sticky="w", padx=2, pady=2)
        wm_size_spinbox = ttk.Spinbox(text_wm_subframe, from_=8, to=1000, textvariable=self.watermark_font_size, width=5,
                                      command=self.record_text_wm_change) # Record on spin click
        wm_size_spinbox.grid(row=2, column=1, sticky="w", padx=2, pady=2)
        wm_size_spinbox.bind("<FocusOut>", self.record_text_wm_change) # Record on focus out
        wm_size_spinbox.bind("<Return>", self.record_text_wm_change) # Record on enter
        ToolTip(wm_size_spinbox, "Font size for text watermark.")

        wm_color_button = ttk.Button(text_wm_subframe, text="Color", width=6, command=self.choose_watermark_color_action) # Use action
        wm_color_button.grid(row=2, column=2, sticky="w", padx=(5, 2), pady=2)
        ToolTip(wm_color_button, "Choose text color.")

        ttk.Label(text_wm_subframe, text="Opacity:").grid(row=3, column=0, sticky="w", padx=2, pady=2)
        wm_opacity_scale = ttk.Scale(text_wm_subframe, from_=0, to=255, variable=self.watermark_opacity, orient=tk.HORIZONTAL,
                                     command=self.update_preview_debounced) # Live update ok
        wm_opacity_scale.bind("<ButtonRelease-1>", self.record_text_wm_change) # Record for undo on release
        wm_opacity_scale.grid(row=3, column=1, columnspan=2, sticky="ew", padx=2, pady=2)
        ToolTip(wm_opacity_scale, "Text opacity (0=transparent, 255=opaque).")

        ttk.Label(text_wm_subframe, text="Position:").grid(row=4, column=0, sticky="w", padx=2, pady=2)
        wm_pos_values = ["Center", "Top Left", "Top Right", "Bottom Left", "Bottom Right", "Tile", "Diagonal Fit"]
        wm_pos_combo = ttk.Combobox(text_wm_subframe, textvariable=self.watermark_position, state="readonly", values=wm_pos_values)
        wm_pos_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=2, pady=2)
        wm_pos_combo.bind("<<ComboboxSelected>>", self.record_text_wm_change) # Record for undo
        ToolTip(wm_pos_combo, "Position of the text watermark. 'Diagonal Fit' attempts to size and rotate.")

        # --- Image Watermark Sub-Frame ---
        image_wm_subframe = ttk.LabelFrame(watermark_frame, text="Image Watermark (Global - Manual Placement)", padding=10)
        image_wm_subframe.pack(fill="x", pady=(0, 10))
        image_wm_subframe.columnconfigure(1, weight=1)

        ttk.Checkbutton(image_wm_subframe, text="Enable Image Watermark", variable=self.use_image_watermark,
                        command=self.toggle_image_wm_action).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)) # Use action

        ttk.Label(image_wm_subframe, text="File:").grid(row=1, column=0, sticky="w", padx=2, pady=2)
        # Assign to self to allow drop target registration
        self.wm_img_entry = ttk.Entry(image_wm_subframe, textvariable=self.watermark_image_path, state="readonly")
        self.wm_img_entry.grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ToolTip(self.wm_img_entry, "Path to the global watermark image file. Drop a single image file here to set it.")
        wm_img_browse = ttk.Button(image_wm_subframe, text="Browse", width=7, command=self.browse_watermark_image_action) # Use action
        wm_img_browse.grid(row=1, column=2, sticky="w", padx=(5,2), pady=2)
        ToolTip(wm_img_browse, "Select an image file for the watermark for the *current* main image.")

        ttk.Label(image_wm_subframe, text="Opacity:").grid(row=2, column=0, sticky="w", padx=2, pady=2)
        # Opacity needs to be applied per-image if it's a per-image setting. Keep variable for UI link.
        self.wm_image_opacity_scale = ttk.Scale(image_wm_subframe, from_=0, to=255, variable=self.watermark_image_opacity, orient=tk.HORIZONTAL,
                                         command=self.update_preview_debounced) # Live update ok
        self.wm_image_opacity_scale.bind("<ButtonRelease-1>", self.record_image_wm_change) # Record for undo
        self.wm_image_opacity_scale.grid(row=2, column=1, columnspan=2, sticky="ew", padx=2, pady=2)
        ToolTip(self.wm_image_opacity_scale, "Watermark image opacity (applied when interacting).")

        # Remove Placement Combobox - It's always "Manual" for this per-image watermark now.
        # Add button to reset placement
        reset_wm_place_button = ttk.Button(image_wm_subframe, text="Reset Placement", command=self.reset_image_wm_placement_action) # Use action
        reset_wm_place_button.grid(row=3, column=0, columnspan=3, padx=5, pady=5, sticky="ew")
        ToolTip(reset_wm_place_button, "Reset the current image watermark's size, position, and rotation.")


        # --- Tab 5: Overlays ---
        overlay_frame = ttk.Frame(self.processing_notebook, padding="10")
        self.processing_notebook.add(overlay_frame, text=" Overlays ")
        overlay_frame.columnconfigure(0, weight=1) # Listbox expands
        overlay_frame.rowconfigure(1, weight=1) # Listbox expands

        # Controls Frame
        overlay_controls_frame = ttk.Frame(overlay_frame)
        overlay_controls_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        overlay_controls_frame.columnconfigure(1, weight=1) # Make slider expand

        add_overlay_button = ttk.Button(overlay_controls_frame, text="Add Overlay", command=self.add_overlay_action)
        add_overlay_button.grid(row=0, column=0, padx=2, pady=2)
        ToolTip(add_overlay_button, "Browse for an image to add as a new overlay layer.")

        ttk.Label(overlay_controls_frame, text="Opacity:").grid(row=0, column=1, padx=(10, 2), pady=2, sticky='e')
        self.overlay_opacity_var = tk.IntVar(value=128) # Variable to link scale to selection
        self.overlay_opacity_scale = ttk.Scale(overlay_controls_frame, from_=0, to=255, variable=self.overlay_opacity_var, orient=tk.HORIZONTAL, state=tk.DISABLED,
                                               command=self.update_preview_debounced)
        self.overlay_opacity_scale.bind("<ButtonRelease-1>", self.record_overlay_opacity_change) # Record for undo
        self.overlay_opacity_scale.grid(row=0, column=2, padx=(0, 5), pady=2, sticky='ew')
        ToolTip(self.overlay_opacity_scale, "Adjust opacity for the *selected* overlay.")


        # Listbox Frame
        overlay_list_frame = ttk.Frame(overlay_frame)
        overlay_list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        overlay_list_frame.rowconfigure(0, weight=1)
        overlay_list_frame.columnconfigure(0, weight=1)

        overlay_scrollbar = ttk.Scrollbar(overlay_list_frame, orient=tk.VERTICAL)
        self.overlay_listbox = tk.Listbox(overlay_list_frame, selectmode=tk.SINGLE, exportselection=False,
                                         yscrollcommand=overlay_scrollbar.set)
        self.overlay_listbox.grid(row=0, column=0, sticky="nsew")
        overlay_scrollbar.config(command=self.overlay_listbox.yview)
        overlay_scrollbar.grid(row=0, column=1, sticky="ns")
        ToolTip(self.overlay_listbox, "List of overlays for the current image. Select one to edit, or drag & drop image files onto this list to add them.")
        self.overlay_listbox.bind("<<ListboxSelect>>", self.on_overlay_select)
        # Add hover effect binding for overlay listbox
        if _tkdnd_available:
            self.overlay_listbox.bind("<Enter>", self.on_dnd_enter)
            self.overlay_listbox.bind("<Leave>", self.on_dnd_leave)
        # Add hover effect binding for overlay listbox
        if _tkdnd_available:
            self.overlay_listbox.bind("<Enter>", self.on_dnd_enter)
            self.overlay_listbox.bind("<Leave>", self.on_dnd_leave)
        # Add hover effect binding for overlay listbox
        if _tkdnd_available:
            self.overlay_listbox.bind("<Enter>", self.on_dnd_enter)
            self.overlay_listbox.bind("<Leave>", self.on_dnd_leave)
        ToolTip(self.overlay_listbox, "List of overlays for the current image. Select one to edit, or drag & drop image files onto this list to add them.")


        # Order/Remove Buttons Frame
        overlay_buttons_frame = ttk.Frame(overlay_frame)
        overlay_buttons_frame.grid(row=1, column=1, sticky="ns", padx=(5, 0))

        overlay_up_button = ttk.Button(overlay_buttons_frame, text="▲", width=3, command=lambda: self.change_overlay_order_action("up"))
        overlay_up_button.pack(pady=2)
        ToolTip(overlay_up_button, "Move selected overlay up (render later/on top).")

        overlay_down_button = ttk.Button(overlay_buttons_frame, text="▼", width=3, command=lambda: self.change_overlay_order_action("down"))
        overlay_down_button.pack(pady=2)
        ToolTip(overlay_down_button, "Move selected overlay down (render earlier/below).")

        overlay_remove_button = ttk.Button(overlay_buttons_frame, text="X", width=3, command=self.remove_selected_overlay_action)
        overlay_remove_button.pack(pady=(10,2))
        ToolTip(overlay_remove_button, "Remove selected overlay.")


        # --- Right Preview Panel ---
        preview_outer_frame = ttk.Frame(main_frame, padding=0) # No padding here
        preview_outer_frame.grid(row=0, column=1, sticky="nsew", pady=0)
        preview_outer_frame.rowconfigure(0, weight=1) # Canvas expands
        preview_outer_frame.columnconfigure(0, weight=1) # Canvas expands

        # Use a standard tk Frame for the canvas parent to avoid potential theme background issues
        preview_canvas_frame = tk.Frame(preview_outer_frame, background="gray50")
        preview_canvas_frame.grid(row=0, column=0, sticky="nsew")
        preview_canvas_frame.rowconfigure(0, weight=1)
        preview_canvas_frame.columnconfigure(0, weight=1)

        self.preview_canvas = tk.Canvas(preview_canvas_frame, background="gray50", relief="sunken", borderwidth=1, highlightthickness=0)
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas_image_id = None # ID of the main image on canvas

        # Bind Mouse Events
        self.preview_canvas.bind("<Configure>", self.on_canvas_resize_debounced) # Handle window resize
        self.preview_canvas.bind("<ButtonPress-1>", self.on_mouse_press)
        self.preview_canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.preview_canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        # Zoom
        if sys.platform == "darwin": # MacOS uses different binding for scroll wheel
             self.preview_canvas.bind("<Command-MouseWheel>", self.on_mouse_wheel_zoom) # Cmd+Scroll
             self.preview_canvas.bind("<Option-ButtonPress-1>", self.on_pan_press) # Option+Click for Pan Start
             self.preview_canvas.bind("<Option-B1-Motion>", self.on_pan_drag)
             self.preview_canvas.bind("<Option-ButtonRelease-1>", self.on_pan_release)
        else: # Windows/Linux
             self.preview_canvas.bind("<Control-MouseWheel>", self.on_mouse_wheel_zoom) # Ctrl+Scroll
             self.preview_canvas.bind("<ButtonPress-2>", self.on_pan_press) # Middle Mouse for Pan Start
             self.preview_canvas.bind("<B2-Motion>", self.on_pan_drag)
             self.preview_canvas.bind("<ButtonRelease-2>", self.on_pan_release)

        ToolTip(self.preview_canvas, "Image preview. Ctrl+Scroll=Zoom. MiddleClick/Alt+Drag=Pan. Drag=Select. Click/Drag Edits/Watermarks/Overlays.")

        # Info/Zoom controls below canvas
        preview_info_frame = ttk.Frame(preview_outer_frame, padding=(5, 2))
        preview_info_frame.grid(row=1, column=0, sticky="ew")
        preview_info_frame.columnconfigure(1, weight=1) # Let info label expand

        self.image_info_label = ttk.Label(preview_info_frame, text="Load an image.", anchor=tk.W)
        self.image_info_label.grid(row=0, column=1, sticky="ew", padx=(5,0))

        self.zoom_label = ttk.Label(preview_info_frame, text="Zoom: 100%", style="Zoom.TLabel", anchor=tk.W)
        self.zoom_label.grid(row=1, column=1, sticky="w", padx=(5,0))

        zoom_button_frame = ttk.Frame(preview_info_frame)
        zoom_button_frame.grid(row=0, column=0, rowspan=2, sticky="w") # Span 2 rows, align left

        zoom_in_button = ttk.Button(zoom_button_frame, text="+", width=3, command=self.zoom_in)
        zoom_in_button.pack(side=tk.LEFT, padx=1)
        ToolTip(zoom_in_button, "Zoom In")
        zoom_out_button = ttk.Button(zoom_button_frame, text="-", width=3, command=self.zoom_out)
        zoom_out_button.pack(side=tk.LEFT, padx=1)
        ToolTip(zoom_out_button, "Zoom Out")
        zoom_fit_button = ttk.Button(zoom_button_frame, text="Fit", width=4, command=self.zoom_fit)
        zoom_fit_button.pack(side=tk.LEFT, padx=1)
        ToolTip(zoom_fit_button, "Fit Image to View")
        zoom_100_button = ttk.Button(zoom_button_frame, text="100%", width=5, command=self.zoom_100)
        zoom_100_button.pack(side=tk.LEFT, padx=1)
        ToolTip(zoom_100_button, "Zoom to 100% (Actual Pixels)")


        # --- Image List Notebook (Batch Mode) ---
        self.image_notebook = ttk.Notebook(preview_outer_frame)
        self.image_notebook.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        preview_outer_frame.rowconfigure(2, weight=0) # Don't let notebook expand vertically excessively initially
        self.image_notebook.bind("<<NotebookTabChanged>>", self.on_image_tab_change)
        # Initially hidden if no images
        self.image_notebook.grid_remove()

        # --- Status Bar ---
        status_frame = ttk.Frame(self.root, padding=(10, 5))
        status_frame.grid(row=1, column=0, sticky="sew") # Span across bottom
        status_frame.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(status_frame, text="Ready.", anchor=tk.W)
        self.status_label.grid(row=0, column=0, sticky="ew")

        self.progress_bar = ttk.Progressbar(status_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        # --- Bind Undo/Redo Keys ---
        self.root.bind_all("<Control-z>", self.undo)
        self.root.bind_all("<Control-y>", self.redo)
        # MacOS specific Ctrl+Shift+Z for redo might be needed? Testing required.
        if sys.platform == "darwin":
            self.root.bind_all("<Command-z>", self.undo)
            self.root.bind_all("<Command-Shift-z>", self.redo) # Cmd+Shift+Z is common Mac redo

        # --- Register Additional Drop Targets ---
        if _tkdnd_available:
            try:
                # Watermark Entry Drop Target
                self.wm_img_entry.drop_target_register(DND_FILES)
                self.wm_img_entry.dnd_bind('<<Drop>>', self.handle_watermark_drop)

                # Overlay Listbox Drop Target
                self.overlay_listbox.drop_target_register(DND_FILES)
                self.overlay_listbox.dnd_bind('<<Drop>>', self.handle_overlay_drop)
            except Exception as e:
                print(f"Error registering additional drop targets: {e}")

    # --- Drag and Drop UI Feedback (Generalized) ---
    def on_dnd_enter(self, event):
        """Change background and cursor on entering ANY DND-enabled widget."""
        if not _tkdnd_available:
            return
        widget = event.widget
        try:
            # Ensure widget exists
            if not widget.winfo_exists():
                return

            # Store original background if not already stored for this widget
            widget_id = str(widget)
            if widget_id not in self._original_widget_bgs:
                 # Use actual current background, handling potential errors
                 try:
                     current_bg = widget.cget("background")
                     self._original_widget_bgs[widget_id] = current_bg
                 except tk.TclError:
                     # Fallback if cget fails (e.g., some complex widgets like Listbox)
                     # For Listbox, background might be style-dependent or default.
                     # Using a generic fallback is safer than potentially erroring.
                     self._original_widget_bgs[widget_id] = "SystemWindow" # A common listbox bg

            # Apply hover effect
            # Use try-except for config as some widgets might not support 'background' directly
            try:
                widget.config(background="#E0E8F0") # Light blueish background
            except tk.TclError:
                 # Ignore if background config fails (e.g., for Listbox which uses styles)
                 pass
            widget.config(cursor="plus") # Set cursor

            # Special handling for the main DND frame/label pair
            if widget == self.dnd_label:
                if hasattr(self, 'drag_drop_frame') and self.drag_drop_frame.winfo_exists():
                    self.drag_drop_frame.config(cursor="plus")
            elif widget == self.drag_drop_frame:
                 # Also change label bg/cursor if hovering over frame
                 if hasattr(self, 'dnd_label') and self.dnd_label.winfo_exists():
                     label_id = str(self.dnd_label)
                     if label_id not in self._original_widget_bgs:
                         try:
                             self._original_widget_bgs[label_id] = self.dnd_label.cget("background")
                         except tk.TclError:
                             self._original_widget_bgs[label_id] = "SystemButtonFace"
                     try: # Protect label config too
                         self.dnd_label.config(background="#E0E8F0", cursor="plus")
                     except tk.TclError: pass


        except (tk.TclError, AttributeError): # Widget might be destroyed or lack config method
            pass

    def on_dnd_leave(self, event):
        """Revert background and cursor on leaving ANY DND-enabled widget."""
        if not _tkdnd_available:
            return
        widget = event.widget
        try:
            # Ensure widget exists
            if not widget.winfo_exists():
                return

            # Retrieve original background
            widget_id = str(widget)
            # Use a more specific fallback if available, else generic
            default_bg = "SystemWindow" if isinstance(widget, tk.Listbox) else "SystemButtonFace"
            original_bg = self._original_widget_bgs.get(widget_id, default_bg)

            # Revert effect
            try:
                widget.config(background=original_bg)
            except tk.TclError:
                 # Ignore if background config fails
                 pass
            widget.config(cursor="") # Revert cursor

            # Special handling for the main DND frame/label pair
            if widget == self.dnd_label:
                 if hasattr(self, 'drag_drop_frame') and self.drag_drop_frame.winfo_exists():
                    self.drag_drop_frame.config(cursor="")
            elif widget == self.drag_drop_frame:
                 # Also revert label bg/cursor if leaving frame
                 if hasattr(self, 'dnd_label') and self.dnd_label.winfo_exists():
                     label_id = str(self.dnd_label)
                     original_label_bg = self._original_widget_bgs.get(label_id, "SystemButtonFace")
                     try: # Protect label config
                         self.dnd_label.config(background=original_label_bg, cursor="")
                     except tk.TclError: pass

        except (tk.TclError, AttributeError, KeyError): # Widget might be destroyed or lack config
             pass
    # --- End Drag and Drop UI Feedback ---



    # --- UI Update & Validation ---

    def _get_current_image_setting(self, key, default=None):
        """Safely get a setting for the current image."""
        if self.current_image_path and self.current_image_path in self.image_settings:
            return self.image_settings[self.current_image_path].get(key, default)
        return default

    def update_widget_states(self, processing=False):
        """Enable/disable widgets based on application state."""
        try:
            is_image_loaded = bool(self.rotated_flipped_image) # Base check on rotated/flipped image existence
            is_single_image = len(self.image_list) <= 1
            is_jpeg_or_webp = self.format_var.get() in ["JPEG", "WEBP"]
            is_preset_active = self.preset_var.get() != "Custom"
            is_manual_wm_enabled = self.use_image_watermark.get() # Manual placement is now assumed
            is_overlay_selected = bool(self.selected_overlay_uuid)
            is_edit_area_selected = bool(self.selected_area_uuid and self.selected_area_type in ['blur', 'blackout'])


            # General state based on image loaded and processing status
            img_state = tk.NORMAL if is_image_loaded and not processing else tk.DISABLED
            process_lock_state = tk.NORMAL if not processing else tk.DISABLED # For things locked during conversion

            # --- File/Output Widgets ---
            self.filename_entry.config(state=img_state)
            self.output_dir_button.config(state=process_lock_state)
            self.browse_button.config(state=process_lock_state) # Now self.browse_button exists
            # DnD Label State
            dnd_state = tk.NORMAL if _tkdnd_available and not processing else tk.DISABLED
            if hasattr(self, 'dnd_label'):
                try: self.dnd_label.config(state=dnd_state)
                except: pass

            # --- Action Buttons ---
            self.convert_one_button.config(state=img_state)
            self.convert_all_button.config(state=tk.NORMAL if not is_single_image and is_image_loaded and not processing else tk.DISABLED)
            self.reset_button.config(state=process_lock_state)
            self.save_preset_button.config(state=process_lock_state)
            # Undo/Redo state updated separately by update_undo_redo_buttons()

            # Theme Menu
            theme_menu_state = process_lock_state
            if hasattr(self, 'theme_menu') and self.theme_menu.cget('state') == 'disabled':
                 theme_menu_state = 'disabled' # Keep disabled if themes failed initially
            if hasattr(self, 'theme_menu'):
                self.theme_menu.config(state=theme_menu_state)


            # --- Processing Notebook Tabs ---
            # Tab: General
            self.preset_menu.config(state=img_state)
            self.format_menu.config(state=img_state)
            self.quality_entry.config(state=tk.NORMAL if is_jpeg_or_webp and img_state == tk.NORMAL else tk.DISABLED)
            self.quality_label.config(state=tk.NORMAL if is_jpeg_or_webp and img_state == tk.NORMAL else tk.DISABLED)
            resize_state = tk.NORMAL if not is_preset_active and img_state == tk.NORMAL else tk.DISABLED
            self.resize_width_entry.config(state=resize_state)
            self.resize_height_entry.config(state=resize_state)
            self.resize_info_label.config(state=resize_state)
            self.filter_menu.config(state=img_state)
            self.rotate_ccw_button.config(state=img_state)
            self.rotate_cw_button.config(state=img_state)
            self.flip_h_button.config(state=img_state)
            self.flip_v_button.config(state=img_state)

            # Tab: Adjustments
            for widget in self.root.nametowidget(self.processing_notebook.tabs()[1]).winfo_children(): # Adjustments Frame
                 try: widget.config(state=img_state)
                 except tk.TclError: pass # Skip non-state widgets like labels

            # Tab: Edits
            has_manual_edits = bool(self._get_current_image_setting('blur_areas', []) or self._get_current_image_setting('blackout_areas', []))
            self.blur_area_button.config(state=img_state if self.current_selection_original else tk.DISABLED)
            self.blackout_area_button.config(state=img_state if self.current_selection_original else tk.DISABLED)
            self.clear_areas_button.config(state=img_state if has_manual_edits else tk.DISABLED)
            self.edit_remove_button.config(state=img_state if is_edit_area_selected else tk.DISABLED)
            # Radiobuttons and blur scale should follow img_state
            edits_tab_frame = self.root.nametowidget(self.processing_notebook.tabs()[2])
            for child in edits_tab_frame.winfo_children():
                 if isinstance(child, ttk.Frame): # Shape, Blur, Blackout frames
                     for sub_child in child.winfo_children():
                          try: # Radiobuttons, Scales, Labels, Buttons inside frames
                              if sub_child not in [self.blur_area_button, self.blackout_area_button]: # Already handled
                                   sub_child.config(state=img_state)
                          except tk.TclError: pass
                 # Handle direct children like remove/clear buttons (already done above)

            # Tab: Watermarks
            # Text WM (Global controls enabled if image loaded)
            text_wm_frame = self.root.nametowidget(self.processing_notebook.tabs()[3]).winfo_children()[0] # text_wm_subframe
            text_wm_check_state = self.use_text_watermark.get()
            for widget in text_wm_frame.winfo_children():
                 try:
                      if isinstance(widget, ttk.Checkbutton): widget.config(state=img_state)
                      else: widget.config(state=img_state if text_wm_check_state else tk.DISABLED)
                 except tk.TclError: pass

            # Image WM (Per-image controls)
            img_wm_frame = self.root.nametowidget(self.processing_notebook.tabs()[3]).winfo_children()[1] # image_wm_subframe
            img_wm_check_state = self.use_image_watermark.get()
            # Check the GLOBAL watermark info for a path
            has_img_wm_path = bool(self.wm_img_info.get('path'))
            # Determine base state for WM controls (enabled only if not processing)
            wm_base_state = tk.NORMAL if not processing else tk.DISABLED

            for widget in img_wm_frame.winfo_children():
                 try:
                      if isinstance(widget, ttk.Checkbutton):
                          # Checkbox always enabled unless processing
                          widget.config(state=wm_base_state)
                      elif isinstance(widget, ttk.Button) and "Browse" in widget.cget("text"):
                          # Browse enabled if checkbox checked (and not processing)
                          # Browse button should always be enabled unless processing
                          widget.config(state=wm_base_state)
                      elif isinstance(widget, ttk.Button) and "Reset" in widget.cget("text"):
                           # Reset enabled if checkbox checked AND path exists (and not processing)
                           widget.config(state=wm_base_state if img_wm_check_state and has_img_wm_path else tk.DISABLED)
                      elif isinstance(widget, ttk.Scale): # Opacity scale
                           # Opacity enabled if checkbox checked AND path exists (and not processing)
                           widget.config(state=wm_base_state if img_wm_check_state and has_img_wm_path else tk.DISABLED)
                      elif isinstance(widget, ttk.Entry): # Path entry
                           # Path entry enabled (readonly) if checkbox checked (and not processing)
                           widget.config(state="readonly" if wm_base_state == tk.NORMAL and img_wm_check_state else tk.DISABLED)
                      else: # Labels
                           # Labels enabled if checkbox checked (and not processing)
                           widget.config(state=wm_base_state if img_wm_check_state else tk.DISABLED)
                 except tk.TclError: pass

            # Tab: Overlays
            overlays_tab_frame = self.root.nametowidget(self.processing_notebook.tabs()[4])
            has_overlays = bool(self._get_current_image_setting('overlays', []))
            # Find specific controls by checking text/type
            add_overlay_button = None
            overlay_up_button = None
            overlay_down_button = None
            overlay_remove_button = None

            for child in overlays_tab_frame.winfo_children():
                if isinstance(child, ttk.Frame): # Top control frame or button frame
                    for sub_child in child.winfo_children():
                        if isinstance(sub_child, ttk.Button) and "Add Overlay" in sub_child.cget("text"):
                            add_overlay_button = sub_child
                        elif isinstance(sub_child, ttk.Scale): # Opacity scale already handled (self.overlay_opacity_scale)
                            pass
                elif isinstance(child, ttk.Frame): # Side button frame
                     for sub_child in child.winfo_children():
                        if isinstance(sub_child, ttk.Button):
                            if "▲" in sub_child.cget("text"): overlay_up_button = sub_child
                            elif "▼" in sub_child.cget("text"): overlay_down_button = sub_child
                            elif "X" in sub_child.cget("text"): overlay_remove_button = sub_child

            if add_overlay_button: add_overlay_button.config(state=img_state)
            # Opacity scale enabled only if an overlay is selected
            self.overlay_opacity_scale.config(state=img_state if is_overlay_selected else tk.DISABLED)
            # Layer/Remove buttons enabled only if an overlay is selected
            if overlay_up_button: overlay_up_button.config(state=img_state if is_overlay_selected else tk.DISABLED)
            if overlay_down_button: overlay_down_button.config(state=img_state if is_overlay_selected else tk.DISABLED)
            if overlay_remove_button: overlay_remove_button.config(state=img_state if is_overlay_selected else tk.DISABLED)
            # Listbox itself enabled if image loaded
            self.overlay_listbox.config(state=img_state)


            # Preview Zoom Buttons
            for widget in self.zoom_label.master.winfo_children(): # Get frame containing zoom buttons/label
                if isinstance(widget, ttk.Frame): # The button frame
                    for button in widget.winfo_children():
                         try: button.config(state=img_state)
                         except: pass


        except Exception as e:
            print(f"Error updating widget states: {e}")
            import traceback
            traceback.print_exc() # Print full traceback for debugging

    def update_undo_redo_buttons(self):
        """Updates the state of Undo/Redo buttons based on current stacks."""
        if hasattr(self, 'undo_button'): # Ensure buttons exist
             self.undo_button.config(state=tk.NORMAL if self.current_undo_stack else tk.DISABLED)
        if hasattr(self, 'redo_button'):
             self.redo_button.config(state=tk.NORMAL if self.current_redo_stack else tk.DISABLED)

    def validate_quality(self, value_if_allowed):
        if value_if_allowed == "": return True
        try: return 1 <= int(value_if_allowed) <= 100
        except ValueError: return False

    def validate_dimension(self, value_if_allowed):
        if value_if_allowed == "": return True
        try: return int(value_if_allowed) >= 0
        except ValueError: return False

    def on_format_change(self, event=None):
        # This might become an "action" if we want to undo format changes easily,
        # but for now, just update state and preview.
        self.update_widget_states()
        self.update_preview_safe() # Format might affect preview interpretation (e.g., transparency)

    def change_theme_action(self, event=None):
        # Note: Theme change is NOT undoable currently.
        new_theme = self.theme_var.get()
        current_theme = self.themed_style.theme_use()
        if new_theme == current_theme:
            return

        print(f"Attempting to change theme to: {new_theme}")
        try:
            self.themed_style.theme_use(new_theme)
            # Force redraw/update of all widgets
            self.root.update_idletasks()
            # Re-apply styles that might be theme-dependent
            self.themed_style.configure("DND.TLabel", padding=5)
            self.themed_style.configure("Zoom.TLabel", padding=(0, 5), font=("Segoe UI", 8))
            self.themed_style.configure("Header.TLabel", font=("Segoe UI", 9, "bold"))
            self.status_label.config(text=f"Theme changed to {new_theme}.")
            print(f"Theme successfully changed to {new_theme}")
        except Exception as e:
            messagebox.showerror("Theme Error", f"Could not apply theme '{new_theme}':\n{e}")
            print(f"Error applying theme '{new_theme}', reverting to '{current_theme}'. Error: {e}")
            try:
                 self.themed_style.theme_use(current_theme)
                 self.theme_var.set(current_theme)
                 # Re-apply styles for the reverted theme
                 self.themed_style.configure("DND.TLabel", padding=5)
                 self.themed_style.configure("Zoom.TLabel", padding=(0, 5), font=("Segoe UI", 8))
                 self.themed_style.configure("Header.TLabel", font=("Segoe UI", 9, "bold"))
            except Exception as revert_e:
                 print(f"Failed to revert theme: {revert_e}. Trying 'clam'.")
                 try:
                     self.themed_style.theme_use("clam")
                     self.theme_var.set("clam")
                 except:
                     print("Could not apply any theme.")


    # --- File Handling ---
    def handle_drop(self, event):
        if not _tkdnd_available:
            messagebox.showinfo("Drag and Drop Disabled", "Drag and Drop is disabled.")
            return

        try:
            raw_data = event.data
            # --- New Path Parsing Logic ---
            files = []
            # Try regex to find paths enclosed in curly braces first
            # This pattern finds text between the outermost curly braces.
            # It handles multiple {path} {path} occurrences.
            brace_matches = re.findall(r'\{([^{}]+)\}', raw_data)
            if brace_matches:
                # If braces are found, assume each match is a path
                files = [match.strip() for match in brace_matches if match.strip()]
                print(f"Parsed {len(files)} paths using regex (braces): {files}")
            else:
                # Fallback to splitlist if no braces found or pattern doesn't match expected format
                try:
                    # Use splitlist as the primary fallback
                    files_raw = self.root.tk.splitlist(raw_data)
                    # Clean paths: strip whitespace and remove potential empty strings
                    files = [f.strip() for f in files_raw if f.strip()]
                    if files:
                         print(f"Parsed {len(files)} paths using splitlist: {files}")
                    else:
                         # If splitlist gives nothing, maybe it's a single path without braces/spaces?
                         single_path = raw_data.strip()
                         # Avoid adding empty strings if raw_data itself was just whitespace
                         if single_path:
                             files = [single_path]
                             print(f"Parsed 1 path using direct data (fallback): {files}")

                except Exception as split_err:
                    print(f"Error using splitlist on data: {raw_data}. Error: {split_err}")
                    # As a last resort, try a simple split (less reliable for paths with spaces)
                    files = [f.strip() for f in raw_data.split() if f.strip()]
                    print(f"Parsed {len(files)} paths using simple split (last resort): {files}")

            # --- End New Path Parsing Logic ---

            if not files:
                messagebox.showwarning("Drop Error", "Could not identify valid file paths from dropped data.")
                self.status_label.config(text="Drop failed: No paths found.")
                return

            # Append dropped files
            newly_added_files = []
            processed_paths = set(self.image_list) # Track existing

            for item_path in files:
                if not os.path.exists(item_path): continue
                item_path = os.path.normpath(item_path)
                if item_path in processed_paths: continue

                if os.path.isfile(item_path) and self._is_image_file(item_path):
                    if item_path not in processed_paths:
                        newly_added_files.append(item_path)
                        processed_paths.add(item_path)
                elif os.path.isdir(item_path):
                    processed_paths.add(item_path)
                    try:
                        for root_dir, _, filenames in os.walk(item_path):
                            for filename in filenames:
                                full_path = os.path.normpath(os.path.join(root_dir, filename))
                                if full_path not in processed_paths and self._is_image_file(full_path):
                                    newly_added_files.append(full_path)
                                    processed_paths.add(full_path)
                    except OSError as oe:
                        messagebox.showwarning("Directory Error", f"Could not fully read directory:\n{item_path}\n\n{oe}")
                    except Exception as walk_e: print(f"Error walking directory '{item_path}': {walk_e}")

            if newly_added_files:
                was_list_empty = not self.image_list # Check if list was empty BEFORE adding
                self.image_list.extend(newly_added_files) # Append new files
                self._update_image_notebook() # Update notebook first

                if was_list_empty and self.image_list:
                     # Select first tab, which triggers loading via on_image_tab_change
                     try: self.image_notebook.select(0)
                     except tk.TclError: print("Error selecting first tab after drop.")
                else:
                    # If list wasn't empty, just update status/labels
                    self.update_widget_states() # Ensure states are correct after adding files

                self.dnd_label.config(text=f"Added {len(newly_added_files)}. Total: {len(self.image_list)}.")
                self.status_label.config(text=f"Added {len(newly_added_files)} image(s) via drop. Total: {len(self.image_list)}.")
                print(f"Added {len(newly_added_files)} images. Total now: {len(self.image_list)}")
            else:
                # --- MODIFIED MESSAGE ---
                # Check if any files were dropped at all
                if files: # If files were parsed from the drop event
                    msg = "Images already in list or not valid."
                    status_msg = "No new images added (already in list or invalid)."
                    print("No new valid images found to add (duplicates skipped or invalid format).")
                else: # If the drop event didn't even yield file paths
                    msg = "Could not read dropped items."
                    status_msg = "Failed to parse dropped file paths."
                    print("Drop event occurred but no valid file paths were parsed.")

                self.dnd_label.config(text=msg)
                self.status_label.config(text=status_msg)
                # --- END MODIFICATION ---

            # Ensure states are updated even if no files were added
            self.update_widget_states()

        except Exception as e:
            messagebox.showerror("Drop Error", f"An unexpected error occurred processing dropped files:\n{e}")
            self.status_label.config(text="Error processing dropped files.")
    def handle_watermark_drop(self, event):
        """Handles files dropped onto the image watermark entry."""
        if not _tkdnd_available: return
        print("Watermark drop detected.")
        try:
            # --- Use the same robust path parsing logic as handle_drop ---
            raw_data = event.data
            files = []
            brace_matches = re.findall(r'\{([^{}]+)\}', raw_data)
            if brace_matches:
                files = [match.strip() for match in brace_matches if match.strip()]
            else:
                try:
                    files_raw = self.root.tk.splitlist(raw_data)
                    files = [f.strip() for f in files_raw if f.strip()]
                    if not files:
                         single_path = raw_data.strip()
                         if single_path: files = [single_path]
                except Exception as split_err:
                    print(f"Error using splitlist for watermark drop: {split_err}")
                    files = [f.strip() for f in raw_data.split() if f.strip()]

            if not files:
                 messagebox.showwarning("Drop Error", "Could not parse file path from dropped item.")
                 return

            # --- Process the first valid image file ---
            first_image_path = None
            for filepath in files:
                if os.path.isfile(filepath) and self._is_image_file(filepath):
                    first_image_path = filepath
                    break # Only take the first one

            if first_image_path:
                print(f"Processing dropped watermark: {first_image_path}")
                success = self._set_global_watermark(first_image_path)
                if success:
                    self.status_label.config(text=f"Watermark set from drop: {os.path.basename(first_image_path)}")
                # No need to call update_widget_states/update_preview here, _set_global_watermark does it
            else:
                messagebox.showwarning("Drop Error", "No valid image file found in dropped item(s).")

        except Exception as e:
            messagebox.showerror("Drop Error", f"Error processing watermark drop:\n{e}")
            import traceback
            traceback.print_exc()

    def handle_overlay_drop(self, event):
        """Handles files dropped onto the overlay listbox."""
        if not _tkdnd_available: return
        if not self.current_image_path:
             messagebox.showwarning("Drop Error", "Load an image before adding overlays via drop.")
             return
        print("Overlay drop detected.")
        try:
            # --- Use the same robust path parsing logic as handle_drop ---
            raw_data = event.data
            files = []
            brace_matches = re.findall(r'\{([^{}]+)\}', raw_data)
            if brace_matches:
                files = [match.strip() for match in brace_matches if match.strip()]
            else:
                try:
                    files_raw = self.root.tk.splitlist(raw_data)
                    files = [f.strip() for f in files_raw if f.strip()]
                    if not files:
                         single_path = raw_data.strip()
                         if single_path: files = [single_path]
                except Exception as split_err:
                    print(f"Error using splitlist for overlay drop: {split_err}")
                    files = [f.strip() for f in raw_data.split() if f.strip()]

            if not files:
                 messagebox.showwarning("Drop Error", "Could not parse file paths from dropped items.")
                 return

            # --- Process all valid image files ---
            added_count = 0
            for filepath in files:
                if os.path.isfile(filepath) and self._is_image_file(filepath):
                    print(f"Processing dropped overlay: {filepath}")
                    success = self._add_overlay_from_path(filepath) # Call helper
                    if success: added_count += 1

            if added_count > 0:
                self.status_label.config(text=f"Added {added_count} overlay(s) from drop.")
                self._update_overlay_listbox() # Update UI
                self.update_widget_states()
                self.update_preview_safe()
            else:
                messagebox.showwarning("Drop Error", "No valid image files found in dropped item(s).")

        except Exception as e:
            messagebox.showerror("Drop Error", f"Error processing overlay drop:\n{e}")
            import traceback
            traceback.print_exc()

            self.dnd_label.config(text="Drop Error! Try Again.")
        finally:
            # This might be redundant if called above, but ensures state is updated on error
            self.update_widget_states()


    def browse_files(self):
        filetypes = [('Image Files', '*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.webp'), ('All Files', '*.*')]
        try:
            files = filedialog.askopenfilenames(title="Select Image Files", filetypes=filetypes)
            if files:
                # Save state of potentially edited current image before clearing
                self._save_current_image_settings()
                # Reset state completely for the new list
                self.clear_state(clear_image_list=True) # Clear previous list and preview
                self.image_list = list(files)
                self._update_image_notebook() # Update notebook with new list
                # Selection of the first tab will happen automatically if list is not empty
                self.dnd_label.config(text=f"{len(self.image_list)} image(s) loaded.")
                self.status_label.config(text=f"Loaded {len(self.image_list)} image(s) via browse.")
            else:
                self.status_label.config(text="File selection cancelled.")
            self.update_widget_states()
        except Exception as e:
            messagebox.showerror("Browse Error", f"Error browsing files:\n{e}")
            self.status_label.config(text="Error browsing files.")


    def _update_image_notebook(self):
        """Clears and repopulates the image notebook based on self.image_list."""
        # Clear existing tabs
        selected_index = -1
        try:
             # Remember selected index if possible
             selected_index = self.image_notebook.index(self.image_notebook.select())
        except (tk.TclError, AttributeError):
             pass # No selection or notebook not ready

        for tab_id in self.image_notebook.tabs():
             try: self.image_notebook.forget(tab_id)
             except: pass # Ignore errors during potential rapid updates

        # Add new tabs
        for i, img_path in enumerate(self.image_list):
            filename = os.path.basename(img_path)
            tab_frame = ttk.Frame(self.image_notebook) # Dummy frame
            # Store index, path retrieved later
            self.image_notebook.add(tab_frame, text=f"{i+1}: {filename[:25]}{'...' if len(filename)>25 else ''}", padding=2) # Truncate long names

        # Show/hide notebook
        if not self.image_list:
            self.image_notebook.grid_remove()
            self.clear_state() # Clear preview etc. if list is now empty
        else:
            self.image_notebook.grid()
            # Try to re-select previous index, otherwise select 0
            try:
                 new_selection = selected_index if 0 <= selected_index < len(self.image_list) else 0
                 if len(self.image_list) > 0: # Ensure there's something to select
                     self.image_notebook.select(new_selection)
                     # Explicitly call handler if selection didn't change index but content did
                     if new_selection == selected_index:
                         self.on_image_tab_change()
            except tk.TclError:
                  print("Error re-selecting tab after update.")
                  if len(self.image_list) > 0:
                       try: self.image_notebook.select(0)
                       except: pass # Final fallback

    def on_image_tab_change(self, event=None):
        """Handles switching between image tabs in the notebook."""
        try:
            # Guard against errors during rapid tab creation/deletion
            if not self.image_notebook.winfo_exists(): return
            current_tabs = self.image_notebook.tabs()
            if not current_tabs: return # No tabs visible
            selected_tab_id = self.image_notebook.select()
            if not selected_tab_id: return # No tab selected

            selected_tab_index = self.image_notebook.index(selected_tab_id)
        except tk.TclError as e:
            print(f"Error getting selected tab index: {e}")
            return

        if not (0 <= selected_tab_index < len(self.image_list)):
             print(f"Warning: Selected tab index {selected_tab_index} out of bounds for image list (len {len(self.image_list)}).")
             # If out of bounds, maybe the list changed? Clear state.
             self.clear_state()
             self.update_widget_states()
             return

        new_image_path = self.image_list[selected_tab_index]

        # Avoid reloading if the path hasn't actually changed
        if new_image_path == self.current_image_path:
            return

        # 1. Save settings for the *previous* image (if one was loaded)
        self._save_current_image_settings()

        # 2. Load the new image and its settings
        print(f"Switching to image: {os.path.basename(new_image_path)}")
        self.load_image_for_preview(new_image_path) # This will load image and apply its settings

        # 3. Update states (already done in load_image_for_preview)

    def _is_image_file(self, filepath):
        if not isinstance(filepath, str): return False
        try:
            ext = os.path.splitext(filepath)[1].lower()
            # Added more formats just in case
            return ext in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp', '.ico', '.ppm', '.pgm', '.pbm']
        except Exception: return False

    def load_image_for_preview(self, filepath):
        """Loads an image, sets it as original_image, loads/inits settings, and updates preview."""
        if not os.path.exists(filepath):
             messagebox.showerror("Load Error", f"File not found: {filepath}")
             self._handle_load_error(filepath)
             return
        try:
            print(f"Loading image: {filepath}")
            img = Image.open(filepath)

            # Handle EXIF Orientation
            self.original_image = self._apply_exif_orientation(img) # Keep original unmodified

            # Initialize Rotated/Flipped version (starts same as original)
            self.rotated_flipped_image = self.original_image.copy()

            self.current_image_path = filepath

            # --- Load or Initialize Settings for this Image ---
            if self.current_image_path not in self.image_settings:
                print(f"Initializing default settings for {os.path.basename(self.current_image_path)}")
                self.image_settings[self.current_image_path] = self._get_default_image_settings()
            else:
                 print(f"Loading saved settings for {os.path.basename(self.current_image_path)}")
                 # Ensure all keys exist, merging defaults with loaded settings
                 default_settings = self._get_default_image_settings()
                 loaded_settings = self.image_settings[self.current_image_path]
                 # Ensure dequeues are recreated if loaded from JSON lists
                 loaded_settings['undo_stack'] = deque(loaded_settings.get('undo_stack', []), maxlen=self.MAX_UNDO_HISTORY)
                 loaded_settings['redo_stack'] = deque(loaded_settings.get('redo_stack', []))
                 # Ensure overlays have PIL images reloaded if path exists
                 loaded_settings['overlays'] = self._reload_overlay_images(loaded_settings.get('overlays', []))
                 # WM image info is now global, no need to load per image

                 default_settings.update(loaded_settings) # Merge loaded over defaults
                 self.image_settings[self.current_image_path] = default_settings


            # --- Apply loaded settings to UI and internal state ---
            self._apply_loaded_settings_to_ui()

            # Apply initial transforms from loaded settings
            self._apply_image_transforms_from_settings()

            # --- Update UI based on loaded image ---
            base_name = os.path.splitext(os.path.basename(filepath))[0]
            self.filename_var.set(base_name if len(self.image_list) == 1 else f"{base_name}_<#>")

            # Reset interaction states
            self._reset_interaction_states()

            # Reset Zoom/Pan for new image
            self.zoom_factor = 1.0
            self.pan_offset = [0, 0]
            self._update_zoom_label()

            self.update_preview() # Create and display the preview with loaded settings
            self.zoom_fit() # Automatically fit the image to the preview area on load
            self.update_widget_states() # Update enable/disable states
            self.update_undo_redo_buttons() # Update based on loaded stacks

        except FileNotFoundError:
             messagebox.showerror("Image Load Error", f"File not found: {filepath}")
             self._handle_load_error(filepath)
        except UnidentifiedImageError:
             messagebox.showerror("Image Load Error", f"Cannot identify image file (may be corrupt or unsupported format): {filepath}")
             self._handle_load_error(filepath)
        except Exception as e:
            messagebox.showerror("Image Load Error", f"Failed to load image: {os.path.basename(filepath)}\nError: {e}")
            import traceback
            traceback.print_exc()
            self._handle_load_error(filepath)

    def _apply_exif_orientation(self, img):
        """Applies EXIF orientation tag to the image and returns the corrected image."""
        try:
            exif = img.getexif()
            orientation_tag = 274 # Standard EXIF orientation tag
            if orientation_tag in exif:
                orientation = exif[orientation_tag]
                print(f"Found EXIF Orientation: {orientation}")
                if orientation == 2: img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                elif orientation == 3: img = img.transpose(Image.Transpose.ROTATE_180)
                elif orientation == 4: img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                elif orientation == 5: img = img.transpose(Image.Transpose.ROTATE_90).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                elif orientation == 6: img = img.transpose(Image.Transpose.ROTATE_270)
                elif orientation == 7: img = img.transpose(Image.Transpose.ROTATE_270).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                elif orientation == 8: img = img.transpose(Image.Transpose.ROTATE_90)
        except (AttributeError, KeyError, IndexError, TypeError):
            # cases: image doesn't have getexif or exif data, or orientation tag is missing/invalid
            pass
        except Exception as e:
             print(f"Error applying EXIF orientation: {e}")
        return img

    def _get_default_image_settings(self):
        """Returns a dictionary with default settings for a new image."""
        return {
            'rotation': 0, # Store cumulative rotation
            'flip_h': False,
            'flip_v': False,
            'blur_areas': [], # List of {'uuid': str, 'shape': str, 'coords': tuple, 'strength': int}
            'blackout_areas': [], # List of {'uuid': str, 'shape': str, 'coords': tuple}
            'adjustments': {'brightness': 1.0, 'contrast': 1.0, 'saturation': 1.0},
            # 'wm_img_info' is now global, removed from per-image settings
            'overlays': [], # List of {'uuid': str, 'path': str, 'pil_image': None, 'rect': tuple, 'angle': float, 'opacity': int}
            'undo_stack': deque(maxlen=self.MAX_UNDO_HISTORY),
            'redo_stack': deque()
        }

    def _reload_overlay_images(self, overlays):
        """Tries to reload PIL images for overlays based on stored paths."""
        reloaded_overlays = []
        for overlay in overlays:
            # Ensure pil_image is None initially or if path changed/invalid
            overlay['pil_image'] = None
            path = overlay.get('path')
            if path and os.path.exists(path):
                try:
                    overlay['pil_image'] = Image.open(path).convert("RGBA")
                except Exception as e:
                    print(f"Warning: Could not reload overlay image '{os.path.basename(path)}': {e}")
                    overlay['path'] = None # Clear path if reload fails
            else:
                 overlay['path'] = None # Clear path if it doesn't exist
            reloaded_overlays.append(overlay)
        return reloaded_overlays

    def _reload_wm_image(self, wm_info):
        """Tries to reload PIL image for the main watermark."""
        wm_info['pil_image'] = None
        path = wm_info.get('path')
        if path and os.path.exists(path):
            try:
                wm_info['pil_image'] = Image.open(path).convert("RGBA")
            except Exception as e:
                print(f"Warning: Could not reload watermark image '{os.path.basename(path)}': {e}")
                wm_info['path'] = None
        else:
             wm_info['path'] = None
        return wm_info

    def _apply_loaded_settings_to_ui(self):
         """Updates UI controls based on the currently loaded image's settings."""
         if not self.current_image_path or self.current_image_path not in self.image_settings:
             return

         settings = self.image_settings[self.current_image_path]

         # Apply Adjustments
         adj = settings.get('adjustments', {'brightness': 1.0, 'contrast': 1.0, 'saturation': 1.0})
         self.brightness_var.set(adj.get('brightness', 1.0))
         self.contrast_var.set(adj.get('contrast', 1.0))
         self.saturation_var.set(adj.get('saturation', 1.0))

         # Apply Image WM state (Path, Opacity, Checkbox)
         # Apply GLOBAL Image WM state (Path, Opacity, Checkbox)
         wm_info = self.wm_img_info # Use global watermark info
         self.watermark_image_path.set(wm_info.get('path', "") or "") # Set to empty string if None
         self.watermark_image_opacity.set(wm_info.get('opacity', 128))
         # Enable checkbox if a path exists in the GLOBAL WM info
         self.use_image_watermark.set(bool(wm_info.get('path')))

         # Update Overlays Listbox
         self._update_overlay_listbox()
         # Reset overlay selection and opacity scale
         self.selected_overlay_uuid = None
         self.overlay_opacity_var.set(128) # Default opacity for scale display

         # Update Undo/Redo Stacks reference
         self.current_undo_stack = settings['undo_stack']
         self.current_redo_stack = settings['redo_stack']


    def _handle_load_error(self, filepath=None):
        """Helper to reset state after a load error and remove bad file."""
        # Remove the faulty image from the list
        if filepath and filepath in self.image_list:
            try:
                idx = self.image_list.index(filepath)
                self.image_list.pop(idx)
                if filepath in self.image_settings:
                    del self.image_settings[filepath]
                print(f"Removed faulty image from list: {filepath}")
                # Update the notebook (might trigger tab change)
                self._update_image_notebook() # This should handle selecting a new tab if needed
            except ValueError:
                pass # Image wasn't in list anyway

        # If the list is now empty or the error happened without a specific file context
        if not self.image_list or not filepath:
            self.original_image = None
            self.rotated_flipped_image = None
            self.processed_image = None
            self.preview_image_tk = None
            self.current_image_path = None
            self.filename_var.set("")
            self.image_info_label.config(text="Error loading image.")
            self.preview_canvas.delete("all")
            self.current_undo_stack.clear()
            self.current_redo_stack.clear()
            self._reset_interaction_states()
            # Update UI
            self.update_widget_states()
            self.update_undo_redo_buttons()
            self._update_overlay_listbox() # Clear overlays listbox


    def select_output_dir(self):
        try:
            directory = filedialog.askdirectory(title="Select Output Directory")
            if directory:
                self.output_dir.set(directory)
                self.status_label.config(text=f"Output directory set.")
            else:
                self.status_label.config(text="Output directory selection cancelled.")
        except Exception as e:
            messagebox.showerror("Directory Selection Error", f"Error selecting directory:\n{e}")


    # --- Image Processing & Preview (Core Logic) ---

    def update_preview_debounced(self, event=None):
         """Requests a preview update after a short delay."""
         if self._preview_update_job:
              self.root.after_cancel(self._preview_update_job)
         self._preview_update_job = self.root.after(150, self.update_preview_safe) # 150ms delay

    def update_preview_safe(self, event=None):
        """Safely triggers preview update, handling potential errors."""
        # Cancel any pending debounced update if called directly
        if self._preview_update_job:
              self.root.after_cancel(self._preview_update_job)
              self._preview_update_job = None

        if self.rotated_flipped_image: # Check if base image exists
            try:
                self.update_preview()
            except Exception as e:
                print(f"Error updating preview: {e}")
                import traceback
                traceback.print_exc()
                self.status_label.config(text=f"Preview Error: {e}")
                # Optionally show a message box, but can be annoying
                # messagebox.showerror("Preview Error", f"Error updating preview:\n{e}")
        else:
            # Clear canvas if no image is loaded
            self.preview_canvas.delete("all")
            self.canvas_image_id = None
            self.preview_image_tk = None # Crucial: release reference
            self.processed_image = None # Crucial: release reference
            self.image_info_label.config(text="Load an image to see preview and info.")
        self.update_widget_states() # Ensure widgets reflect current state

    def update_preview(self):
        """Generates and displays the processed preview image on the canvas."""
        if not self.rotated_flipped_image or not self.current_image_path:
            # print("Update Preview cancelled: No rotated/flipped image or path")
            return

        start_time = time.time()

        # --- Get Current Image Settings ---
        settings = self.image_settings.get(self.current_image_path)
        if not settings:
            print("Update Preview cancelled: Settings not found for current path")
            return # Should not happen if loaded correctly

        # Start with the current state of the rotated/flipped image
        img = self.rotated_flipped_image.copy()

        # --- Apply Settings to Generate Processed Image ---
        # Order: Filter -> Resize -> Adjustments -> Manual Edits -> Watermarks -> Overlays

        # 1. Apply Filter (Now per-image for Undo)
        img = self.apply_filter(img, settings.get('filter', 'None')) # Use per-image filter setting

        # 2. Apply Resize (Global Setting / Preset)
        img = self.apply_resize(img, self.preset_var.get(), self.resize_width_var.get(), self.resize_height_var.get())

        # 3. Apply Adjustments (Per Image Setting)
        adj_settings = settings.get('adjustments', {'brightness': 1.0, 'contrast': 1.0, 'saturation': 1.0})
        img = self.apply_adjustments(img, adj_settings)

        # Store the size of the image at this stage (after filter/resize/adjustments)
        self.processed_base_size = img.size

        # Ensure RGBA if subsequent steps need it
        needs_rgba = (settings.get('blur_areas') or settings.get('blackout_areas') or
                      self.use_text_watermark.get() or settings.get('wm_img_info', {}).get('path') or
                      settings.get('overlays'))
        if needs_rgba and img.mode != 'RGBA':
             img = img.convert('RGBA')

        # 4. Apply Manual Edits (Blur/Blackout) (Per Image Setting)
        img = self.apply_manual_edits(img, settings.get('blur_areas', []), settings.get('blackout_areas', []))

        # 5. Apply Text Watermark (Global Setting - if enabled)
        if self.use_text_watermark.get() and self.watermark_text.get():
            img = self.apply_text_watermark(img) # Uses global settings from UI

        # 6. Apply Main Image Watermark (Per Image Setting - if enabled and path exists)
        wm_info = self.wm_img_info # Use global watermark info
        if self.use_image_watermark.get() and wm_info and wm_info.get('path'): # Checkbox AND path needed
            # Apply using the *specific* settings stored for this image
            img = self.apply_single_image_overlay(img, wm_info)

        # 7. Apply Overlays (Per Image Setting)
        overlays = settings.get('overlays', [])
        img = self.apply_overlays(img, overlays) # Apply all overlays in order

        # Store the final processed image for coordinate mapping and display
        self.processed_image = img

        # Update Info Label
        self.image_info_label.config(text=f"Current: {os.path.basename(self.current_image_path)} ({self.original_image.width}x{self.original_image.height}) | Preview: {img.width}x{img.height}")

        # --- Display the processed image on the canvas ---
        self.display_image_on_canvas(self.processed_image)

        end_time = time.time()
        print(f"Preview updated in {end_time - start_time:.4f} seconds.")


    def display_image_on_canvas(self, img_to_display):
        """Scales, pans, and displays the given PIL image on the canvas, including overlays."""
        if not hasattr(self, 'preview_canvas') or not self.preview_canvas.winfo_exists():
            # print("Display cancelled: Canvas not ready")
            return

        try:
            canvas_width = self.preview_canvas.winfo_width()
            canvas_height = self.preview_canvas.winfo_height()

            # If canvas size is not determined yet, wait and retry
            if canvas_width <= 1 or canvas_height <= 1:
                # print("Canvas size not ready, rescheduling display.")
                # Schedule a retry after a short delay
                self.root.after(50, lambda: self.display_image_on_canvas(img_to_display))
                return

            img_w, img_h = img_to_display.size
            if img_w <= 0 or img_h <= 0:
                 print("Display cancelled: Invalid image dimensions")
                 return

            # Calculate the dimensions of the fully zoomed image
            zoomed_w = int(img_w * self.zoom_factor)
            zoomed_h = int(img_h * self.zoom_factor)

            # Determine the portion of the *zoomed* image visible on the canvas
            # Top-left corner of visible area in zoomed image coordinates
            visible_x0_zoomed = -self.pan_offset[0]
            visible_y0_zoomed = -self.pan_offset[1]
            # Bottom-right corner
            visible_x1_zoomed = visible_x0_zoomed + canvas_width
            visible_y1_zoomed = visible_y0_zoomed + canvas_height

            # Determine the corresponding portion in the *original processed* image coordinates
            visible_x0_proc = max(0, int(visible_x0_zoomed / self.zoom_factor))
            visible_y0_proc = max(0, int(visible_y0_zoomed / self.zoom_factor))
            visible_x1_proc = min(img_w, int(visible_x1_zoomed / self.zoom_factor))
            visible_y1_proc = min(img_h, int(visible_y1_zoomed / self.zoom_factor))

            # Check if the visible area has valid dimensions
            if visible_x1_proc <= visible_x0_proc or visible_y1_proc <= visible_y0_proc:
                # print("Display skipped: No visible area.")
                # Clear canvas if nothing is visible? Or leave old image? Clear for now.
                self.preview_canvas.delete("all")
                self.preview_image_tk = None
                return

            # Crop the *original processed* image to the visible portion
            visible_img_pil = img_to_display.crop((visible_x0_proc, visible_y0_proc, visible_x1_proc, visible_y1_proc))

            # Calculate the size of this cropped portion when *zoomed*
            display_w = int(visible_img_pil.width * self.zoom_factor)
            display_h = int(visible_img_pil.height * self.zoom_factor)

            if display_w <=0 or display_h <= 0:
                # print("Display skipped: Calculated display size is zero.")
                self.preview_canvas.delete("all")
                self.preview_image_tk = None
                return

            # Resize the *cropped* portion to its final display size
            # Use NEAREST for performance during zoom/pan, LANCZOS might be too slow? Test needed.
            # LANCZOS provides better quality, let's try it first.
            resample_method = Image.Resampling.LANCZOS
            # resample_method = Image.Resampling.NEAREST if self.zoom_factor > 1 else Image.Resampling.LANCZOS
            try:
                display_img_pil = visible_img_pil.resize((display_w, display_h), resample_method)
            except ValueError:
                print(f"Warning: Resize failed for display ({display_w}x{display_h}). Skipping display.")
                self.preview_canvas.delete("all")
                self.preview_image_tk = None
                return

            # Convert to ImageTk format
            self.preview_image_tk = ImageTk.PhotoImage(display_img_pil)

            # --- Clear previous drawings and draw new image ---
            self.preview_canvas.delete("all") # Clear everything

            # Calculate the canvas coordinates to draw the *visible* (cropped & resized) image
            # This depends on where the top-left of the *visible* portion starts relative to the canvas (0,0)
            canvas_draw_x = int(self.pan_offset[0] + visible_x0_proc * self.zoom_factor)
            canvas_draw_y = int(self.pan_offset[1] + visible_y0_proc * self.zoom_factor)

            # Draw the visible part of the image
            self.canvas_image_id = self.preview_canvas.create_image(
                canvas_draw_x, canvas_draw_y,
                anchor=tk.NW, image=self.preview_image_tk
            )

            # --- Draw Overlays ---
            # Pass current zoom and pan info to drawing functions
            self._draw_manual_edit_overlays()
            self._draw_image_wm_overlay()
            self._draw_overlay_overlays()

            # Re-draw temporary selection rectangle (if mouse is being dragged)
            if self.selection_rect_id and self.selection_start_coords and self.selection_current_coords:
                 x0_c, y0_c = self.selection_start_coords
                 x1_c, y1_c = self.selection_current_coords
                 # Recreate with current coordinates
                 self.selection_rect_id = self.preview_canvas.create_rectangle(x0_c, y0_c, x1_c, y1_c,
                                                                               outline='yellow', dash=(3, 3), width=1, tags="selection")


        except tk.TclError as e:
             # Can happen if widget is destroyed during update
             print(f"TclError displaying image on canvas (likely widget destroyed): {e}")
             self.preview_canvas.delete("all")
             self.preview_image_tk = None
        except Exception as e:
            print(f"Error displaying image on canvas: {e}")
            import traceback
            traceback.print_exc()
            self.status_label.config(text=f"Preview Display Error: {e}")
            self.preview_canvas.delete("all")
            self.preview_image_tk = None

    # --- Coordinate Transformation (Zoom/Pan Aware) ---

    def get_processed_coords(self, canvas_x, canvas_y):
        """Converts canvas coordinates to the coordinates of the *processed_image* (before zoom/pan)."""
        if self.zoom_factor <= 1e-6: return None, None # Avoid division by zero
        try:
            # Account for panning and zooming
            proc_x = (canvas_x - self.pan_offset[0]) / self.zoom_factor
            proc_y = (canvas_y - self.pan_offset[1]) / self.zoom_factor
            return int(proc_x), int(proc_y)
        except Exception as e:
            print(f"Error in get_processed_coords: {e}")
            return None, None

    def get_canvas_coords(self, proc_x, proc_y):
        """Converts *processed_image* coordinates to canvas coordinates (including zoom/pan)."""
        try:
            canvas_x = proc_x * self.zoom_factor + self.pan_offset[0]
            canvas_y = proc_y * self.zoom_factor + self.pan_offset[1]
            return int(canvas_x), int(canvas_y)
        except Exception as e:
            print(f"Error in get_canvas_coords: {e}")
            return None, None

    def original_to_processed_coords(self, orig_x, orig_y):
        """Converts coordinates from the original_image system to the processed_base_size system."""
        if not self.original_image or not self.processed_base_size: return None, None
        try:
            orig_w, orig_h = self.original_image.size
            proc_w, proc_h = self.processed_base_size

            if orig_w <= 0 or orig_h <= 0: return None, None # Avoid division by zero

            scale_x = proc_w / orig_w
            scale_y = proc_h / orig_h

            proc_x = orig_x * scale_x
            proc_y = orig_y * scale_y
            return int(proc_x), int(proc_y)
        except Exception as e:
            print(f"Error in original_to_processed_coords: {e}")
            return None, None

    def processed_to_original_coords(self, proc_x, proc_y):
        """Converts coordinates from the processed_base_size system to the original_image system."""
        if not self.original_image or not self.processed_base_size: return None, None
        try:
            orig_w, orig_h = self.original_image.size
            proc_w, proc_h = self.processed_base_size

            if proc_w <= 0 or proc_h <= 0: return None, None # Avoid division by zero

            scale_x = orig_w / proc_w
            scale_y = orig_h / proc_h

            original_x = proc_x * scale_x
            original_y = proc_y * scale_y
            return int(original_x), int(original_y)
        except Exception as e:
            print(f"Error in processed_to_original_coords: {e}")
            return None, None

    def canvas_to_original_coords(self, canvas_x, canvas_y):
        """Converts canvas coordinates directly to original_image coordinates."""
        # Canvas -> Processed (Zoom/Pan aware) -> Original
        proc_x, proc_y = self.get_processed_coords(canvas_x, canvas_y)
        if proc_x is None: return None, None
        # Convert from processed_image coords to processed_base coords (usually the same unless overlays change size?)
        # Let's assume processed_image size matches processed_base_size for this conversion step.
        # If overlays could change the final size, this needs adjustment.
        # For now: proc_base_x = proc_x, proc_base_y = proc_y
        return self.processed_to_original_coords(proc_x, proc_y)


    def original_to_canvas_coords(self, orig_x, orig_y):
        """Converts original_image coordinates directly to canvas coordinates."""
        # Original -> Processed_Base -> Processed -> Canvas (Zoom/Pan aware)
        proc_base_x, proc_base_y = self.original_to_processed_coords(orig_x, orig_y)
        if proc_base_x is None: return None, None
        # Assuming processed_image size is same as processed_base_size here.
        proc_x, proc_y = proc_base_x, proc_base_y
        return self.get_canvas_coords(proc_x, proc_y)

    # --- Drawing Overlays (Refactored for Zoom/Pan) ---

    def _draw_manual_edit_overlays(self):
        """Draws outlines for blur/blackout areas on canvas, zoom/pan aware."""
        if not self.processed_image or not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return

        blur_areas = settings.get('blur_areas', [])
        blackout_areas = settings.get('blackout_areas', [])

        # Draw Blur Areas
        for area in blur_areas:
            uuid, shape, coords_orig, strength = area['uuid'], area['shape'], area['coords'], area['strength']
            is_selected = (self.selected_area_type == 'blur' and self.selected_area_uuid == uuid)
            self._draw_area_shape(shape, coords_orig, "blur", uuid, is_selected)

        # Draw Blackout Areas
        for area in blackout_areas:
             uuid, shape, coords_orig = area['uuid'], area['shape'], area['coords']
             is_selected = (self.selected_area_type == 'blackout' and self.selected_area_uuid == uuid)
             self._draw_area_shape(shape, coords_orig, "blackout", uuid, is_selected)


    def _draw_image_wm_overlay(self):
        """Draws interactive overlay for the main image watermark."""
        if not self.processed_image or not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return

        wm_info = self.wm_img_info # Use global watermark info
        # Draw only if enabled AND path exists AND it's currently selected for interaction
        if self.use_image_watermark.get() and wm_info and wm_info.get('path') and wm_info.get('rect'):
             is_selected = (self.selected_area_type == 'wm' and self.selected_area_uuid == 'main_wm') # Use fixed UUID for main WM
             if is_selected:
                  self._draw_interactive_handles(wm_info['rect'], wm_info['angle'], "wm", "main_wm")


    def _draw_overlay_overlays(self):
        """Draws interactive overlay for the *selected* image overlay."""
        if not self.processed_image or not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings or not self.selected_overlay_uuid: return # Only draw handles for selected

        overlays = settings.get('overlays', [])
        selected_overlay_data = next((o for o in overlays if o['uuid'] == self.selected_overlay_uuid), None)

        if selected_overlay_data and selected_overlay_data.get('rect'):
            self._draw_interactive_handles(selected_overlay_data['rect'], selected_overlay_data['angle'],
                                           "overlay", self.selected_overlay_uuid)


    def _draw_area_shape(self, shape, coords_orig, area_type, uuid, is_selected):
        """Helper to draw a single blur/blackout area outline."""
        try:
            if area_type == 'blur':
                outline_color = "yellow" if is_selected else "#00A0FF" # Blueish
                tags = ("overlay_blur", f"area_{uuid}")
            else: # blackout
                outline_color = "yellow" if is_selected else "#FF6060" # Reddish
                tags = ("overlay_blackout", f"area_{uuid}")

            outline_width = 2 if is_selected else 1
            outline_dash = () if is_selected else (4, 2)

            # Convert original coords to canvas coords
            x0_orig, y0_orig, x1_orig, y1_orig = coords_orig
            cx0, cy0 = self.original_to_canvas_coords(x0_orig, y0_orig)
            cx1, cy1 = self.original_to_canvas_coords(x1_orig, y1_orig)

            if all(c is not None for c in [cx0, cy0, cx1, cy1]):
                # Ensure positive width/height on canvas for drawing
                if cx0 > cx1: cx0, cx1 = cx1, cx0
                if cy0 > cy1: cy0, cy1 = cy1, cy0
                if cx1 - cx0 < 1 or cy1 - cy0 < 1: return # Too small to draw

                if shape == 'rectangle':
                    self.preview_canvas.create_rectangle(cx0, cy0, cx1, cy1,
                                                       outline=outline_color, width=outline_width, dash=outline_dash,
                                                       tags=tags)
                elif shape == 'circle':
                     self.preview_canvas.create_oval(cx0, cy0, cx1, cy1,
                                                    outline=outline_color, width=outline_width, dash=outline_dash,
                                                    tags=tags)
        except Exception as e:
             print(f"Error drawing area shape {uuid}: {e}")


    def _draw_interactive_handles(self, rect_orig, angle_degrees, area_type, uuid):
        """Draws bounding box and handles for WM or selected overlay."""
        try:
            # Use original coords for calculations, convert to canvas only for drawing
            x0_orig, y0_orig, x1_orig, y1_orig = rect_orig
            center_x_orig = (x0_orig + x1_orig) / 2
            center_y_orig = (y0_orig + y1_orig) / 2

            corners_orig = [(x0_orig, y0_orig), (x1_orig, y0_orig), (x1_orig, y1_orig), (x0_orig, y1_orig)]
            corners_rotated_orig = [self._rotate_point(center_x_orig, center_y_orig, px, py, angle_degrees) for px, py in corners_orig]
            corners_canvas = [self.original_to_canvas_coords(px, py) for px, py in corners_rotated_orig]

            if any(c is None for pair in corners_canvas for c in pair):
                # print(f"Warning: Could not get canvas coords for all interactive corners ({uuid}).")
                return

            tag_prefix = "overlay_wm" if area_type == 'wm' else "overlay_layer"
            tags = (tag_prefix, f"area_{uuid}") # Common tag + specific ID

            # Draw the rotated bounding box
            poly_coords = []
            for cx, cy in corners_canvas: poly_coords.extend([cx, cy])
            self.preview_canvas.create_polygon(poly_coords, outline="cyan", fill="", width=1, dash=(5, 3), tags=tags)

            # Draw resize handles at corners
            handle_size = max(3, int(3 / self.zoom_factor)) # Scale handle size slightly with zoom
            handle_tags = ['resize_tl', 'resize_tr', 'resize_br', 'resize_bl']
            for i, (cx, cy) in enumerate(corners_canvas):
                self.preview_canvas.create_rectangle(
                    cx - handle_size, cy - handle_size, cx + handle_size, cy + handle_size,
                    fill="cyan", outline="black", width=1, tags=(f"{tag_prefix}_handle", handle_tags[i], f"handle_{uuid}") # Specific ID tag
                )

            # Draw rotation handle
            top_mid_orig_x = (x0_orig + x1_orig) / 2
            top_mid_orig_y = y0_orig
            handle_offset = max(20, (y1_orig - y0_orig) * 0.1) # Offset in original coords
            rot_handle_orig_x = top_mid_orig_x
            rot_handle_orig_y = top_mid_orig_y - handle_offset
            rot_handle_rotated_orig = self._rotate_point(center_x_orig, center_y_orig, rot_handle_orig_x, rot_handle_orig_y, angle_degrees)
            rh_cx, rh_cy = self.original_to_canvas_coords(rot_handle_rotated_orig[0], rot_handle_rotated_orig[1])

            if rh_cx is not None and rh_cy is not None:
                 top_mid_rotated_orig = self._rotate_point(center_x_orig, center_y_orig, top_mid_orig_x, top_mid_orig_y, angle_degrees)
                 tm_cx, tm_cy = self.original_to_canvas_coords(top_mid_rotated_orig[0], top_mid_rotated_orig[1])
                 if tm_cx is not None and tm_cy is not None:
                      self.preview_canvas.create_line(tm_cx, tm_cy, rh_cx, rh_cy, fill="cyan", tags=tags)

                 rot_handle_size = max(4, int(4 / self.zoom_factor)) # Scale handle size
                 self.preview_canvas.create_oval(
                     rh_cx - rot_handle_size, rh_cy - rot_handle_size, rh_cx + rot_handle_size, rh_cy + rot_handle_size,
                     fill="cyan", outline="black", width=1, tags=(f"{tag_prefix}_handle", "rotate", f"handle_{uuid}") # Specific ID tag
                 )
        except Exception as e:
             print(f"Error drawing interactive handles for {uuid}: {e}")


    # --- Canvas Interaction (Mouse Events) ---

    def _get_element_at_canvas_coords(self, canvas_x, canvas_y):
        """ Checks canvas coordinates for interactive elements (handles, areas)"""
        # Check handles first (they are smaller targets)
        # Find items near the click, adjust tolerance based on zoom?
        tolerance = max(5, int(5 / self.zoom_factor))
        nearby_items = self.preview_canvas.find_overlapping(canvas_x - tolerance, canvas_y - tolerance, canvas_x + tolerance, canvas_y + tolerance)

        for item_id in reversed(nearby_items): # Check topmost first
            tags = self.preview_canvas.gettags(item_id)
            if any(t.startswith("overlay_wm_handle") or t.startswith("overlay_layer_handle") for t in tags):
                handle_type = None
                area_uuid = None
                for tag in tags:
                    if tag in ['resize_tl', 'resize_tr', 'resize_br', 'resize_bl', 'rotate']:
                        handle_type = tag
                    elif tag.startswith("handle_"):
                        area_uuid = tag.split("_", 1)[1]

                if handle_type and area_uuid:
                    # Determine if it's the main WM or an overlay
                    area_type = 'wm' if area_uuid == 'main_wm' else 'overlay'
                    return area_type, area_uuid, handle_type # e.g., 'overlay', 'uuid123', 'resize_tr'

        # If no handle, check for area bodies
        orig_x, orig_y = self.canvas_to_original_coords(canvas_x, canvas_y)
        if orig_x is None: return None, None, None # Cannot map coords

        settings = self.image_settings.get(self.current_image_path)
        if not settings: return None, None, None

        # Check overlays (topmost first - reverse order)
        overlays = settings.get('overlays', [])
        for overlay in reversed(overlays):
            if overlay.get('rect'):
                 if self._is_point_in_rotated_rect(orig_x, orig_y, overlay['rect'], overlay['angle']):
                      return 'overlay', overlay['uuid'], 'drag' # Interaction type is dragging the body

        # Check main Image WM (if enabled and placed)
        wm_info = self.wm_img_info # Use global watermark info
        if self.use_image_watermark.get() and wm_info and wm_info.get('rect'):
            if self._is_point_in_rotated_rect(orig_x, orig_y, wm_info['rect'], wm_info['angle']):
                 return 'wm', 'main_wm', 'drag'

        # Check manual edit areas (blur/blackout)
        blackout_areas = settings.get('blackout_areas', [])
        for area in reversed(blackout_areas):
            if self._is_point_in_area(orig_x, orig_y, area['shape'], area['coords']):
                return 'blackout', area['uuid'], 'drag'

        blur_areas = settings.get('blur_areas', [])
        for area in reversed(blur_areas):
             if self._is_point_in_area(orig_x, orig_y, area['shape'], area['coords']):
                 return 'blur', area['uuid'], 'drag'

        # No interactive element found
        return None, None, None


    def _is_point_in_area(self, orig_x, orig_y, area_shape, coords_orig):
        """Checks if original image coordinates are inside the non-rotated blur/blackout area."""
        if orig_x is None or orig_y is None: return False
        try:
            x0, y0, x1, y1 = map(int, coords_orig)
            if x0 > x1: x0, x1 = x1, x0
            if y0 > y1: y0, y1 = y1, y0

            if area_shape == "rectangle":
                return x0 <= orig_x < x1 and y0 <= orig_y < y1
            elif area_shape == "circle":
                center_x = (x0 + x1) / 2; center_y = (y0 + y1) / 2
                radius_x = abs(x1 - x0) / 2; radius_y = abs(y1 - y0) / 2
                if radius_x <= 0 or radius_y <= 0: return False
                normalized_dist_sq = ((orig_x - center_x) / radius_x)**2 + ((orig_y - center_y) / radius_y)**2
                return normalized_dist_sq <= 1
        except (ValueError, TypeError, IndexError) as e:
            print(f"Error checking point in area ({area_shape}, {coords_orig}): {e}")
        return False

    def _is_point_in_rotated_rect(self, orig_x, orig_y, rect_orig, angle_degrees):
        """ Checks if original coordinates are inside a rotated rectangle (for WM/Overlays). """
        if orig_x is None or orig_y is None or not rect_orig: return False
        try:
            x0, y0, x1, y1 = rect_orig
            center_x = (x0 + x1) / 2
            center_y = (y0 + y1) / 2

            # Rotate the click point *backwards* around the center
            rotated_click_x, rotated_click_y = self._rotate_point(center_x, center_y, orig_x, orig_y, -angle_degrees)

            # Check if the backwards-rotated point is within the *unrotated* rect
            return x0 <= rotated_click_x < x1 and y0 <= rotated_click_y < y1
        except Exception as e:
            print(f"Error checking point in rotated rect: {e}")
            return False


    def on_mouse_press(self, event):
        """Handles mouse button press on the canvas for selection or interaction."""
        if not self.processed_image or self._pan_active: return

        canvas_x, canvas_y = event.x, event.y
        self._reset_interaction_states() # Clear previous interaction/selection state

        # 1. Check if clicking on an existing element (handle or body)
        area_type, area_uuid, interaction = self._get_element_at_canvas_coords(canvas_x, canvas_y)

        if area_type:
            print(f"Starting interaction: Type={area_type}, UUID={area_uuid}, Mode={interaction}")
            self.selected_area_type = area_type
            self.selected_area_uuid = area_uuid
            self.edit_interaction_mode = interaction
            self.edit_drag_mouse_start = (canvas_x, canvas_y)

            # Store initial state for dragging/resizing/rotating
            current_rect, current_angle = self._get_area_rect_angle(area_type, area_uuid)
            if current_rect:
                self.edit_orig_rect_on_drag_start = list(current_rect) # Copy
                self.edit_orig_angle_on_drag_start = current_angle
                cx = (current_rect[0] + current_rect[2]) / 2
                cy = (current_rect[1] + current_rect[3]) / 2
                self.edit_center_on_drag_start = (cx, cy)

                # Store mouse angle relative to center for rotation start
                if interaction == 'rotate':
                    orig_x, orig_y = self.canvas_to_original_coords(canvas_x, canvas_y)
                    if orig_x is not None:
                        self.edit_mouse_start_angle_on_drag = math.atan2(orig_y - cy, orig_x - cx)
                    else:
                         self.edit_interaction_mode = None # Cannot rotate if coords invalid

            # Highlight the selected element (redraw handles/outline)
            self.update_preview_safe()
            # Update overlay opacity scale if an overlay was selected
            self._update_overlay_ui_for_selection()
            # Update remove button state
            self.update_widget_states()
            return # Don't start drawing selection rectangle

        # 2. If no element clicked, start drawing selection rectangle
        self.selection_start_coords = (canvas_x, canvas_y)
        self.clear_selection_rectangle() # Clear previous rectangle if any
        self.selection_rect_id = self.preview_canvas.create_rectangle(
            canvas_x, canvas_y, canvas_x + 1, canvas_y + 1,
            outline='yellow', dash=(3, 3), width=1, tags="selection"
        )
        self.current_selection_original = None # Reset stored selection coords


    def on_mouse_drag(self, event):
        """Handles mouse drag on the canvas for moving/resizing/rotating or drawing selection."""
        if not self.processed_image or self._pan_active: return

        canvas_x, canvas_y = event.x, event.y
        orig_x, orig_y = self.canvas_to_original_coords(canvas_x, canvas_y)

        # --- Handle Interaction with Existing Element ---
        if self.edit_interaction_mode and self.selected_area_uuid:
            if orig_x is None or not self.edit_orig_rect_on_drag_start:
                # print("Interaction drag cancelled: Invalid coords or missing start state.")
                return # Cannot proceed without valid coords or start state

            start_rect = self.edit_orig_rect_on_drag_start
            start_angle = self.edit_orig_angle_on_drag_start
            center_x, center_y = self.edit_center_on_drag_start
            start_width = start_rect[2] - start_rect[0]
            start_height = start_rect[3] - start_rect[1]

            new_rect = list(start_rect) # Copy
            new_angle = start_angle

            if self.edit_interaction_mode == 'drag':
                # Calculate delta in original coords
                start_canvas_x, start_canvas_y = self.edit_drag_mouse_start
                delta_canvas_x = canvas_x - start_canvas_x
                delta_canvas_y = canvas_y - start_canvas_y
                # Need delta in processed coords first
                proc_x_start, proc_y_start = self.get_processed_coords(start_canvas_x, start_canvas_y)
                proc_x_curr, proc_y_curr = self.get_processed_coords(canvas_x, canvas_y)

                if proc_x_start is not None and proc_x_curr is not None:
                     delta_proc_x = proc_x_curr - proc_x_start
                     delta_proc_y = proc_y_curr - proc_y_start
                     # Convert delta in processed space to delta in original space
                     delta_orig_x, delta_orig_y = self.processed_to_original_coords(delta_proc_x, delta_proc_y)
                     delta_orig_x0, delta_orig_y0 = self.processed_to_original_coords(0, 0) # Get origin offset
                     if delta_orig_x is not None and delta_orig_x0 is not None:
                        delta_orig_x -= delta_orig_x0 # Subtract offset
                        delta_orig_y -= delta_orig_y0

                        # Apply delta to original starting position
                        new_rect[0] = start_rect[0] + delta_orig_x
                        new_rect[1] = start_rect[1] + delta_orig_y
                        new_rect[2] = start_rect[2] + delta_orig_x
                        new_rect[3] = start_rect[3] + delta_orig_y
                     else: print("Warning: Could not calculate original delta for drag.")
                else: print("Warning: Could not get processed coords for drag delta.")


            elif self.edit_interaction_mode == 'rotate':
                 # Calculate angle difference based on mouse movement around center
                 current_mouse_angle = math.atan2(orig_y - center_y, orig_x - center_x)
                 start_mouse_angle = self.edit_mouse_start_angle_on_drag
                 delta_angle_rad = current_mouse_angle - start_mouse_angle
                 new_angle = (start_angle + math.degrees(delta_angle_rad)) % 360


            elif self.edit_interaction_mode.startswith('resize_'):
                 # --- Resize Logic (maintaining aspect ratio) ---
                 # Rotate current mouse point backwards to align with original axes
                 relative_x, relative_y = self._rotate_point(center_x, center_y, orig_x, orig_y, -start_angle)

                 # Calculate distance from center to this relative point
                 dx = relative_x - center_x
                 dy = relative_y - center_y

                 # Determine new size based on projection (simplified: use diagonal dist)
                 orig_corner_dist_sq = (start_width / 2)**2 + (start_height / 2)**2
                 current_dist_sq = dx**2 + dy**2

                 if orig_corner_dist_sq > 1e-9: # Avoid div by zero / sqrt neg
                     scale_factor = math.sqrt(max(0, current_dist_sq / orig_corner_dist_sq))
                     new_width = max(5, start_width * scale_factor) # Min size 5px
                     new_height = max(5, start_height * scale_factor)

                     # Maintain aspect ratio based on original
                     if start_width > 1e-6 and start_height > 1e-6: # Avoid division by zero
                         if start_width > start_height:
                             new_height = new_width * (start_height / start_width)
                         elif start_height > start_width:
                             new_width = new_height * (start_width / start_height)
                     else: # Handle zero start size? Keep scale factor
                         new_height = max(5, start_height * scale_factor)


                     # Keep center fixed during resize for simplicity
                     new_rect[0] = center_x - new_width / 2
                     new_rect[1] = center_y - new_height / 2
                     new_rect[2] = center_x + new_width / 2
                     new_rect[3] = center_y + new_height / 2


            # --- Update the actual element's state in image_settings ---
            self._update_area_state(self.selected_area_type, self.selected_area_uuid, tuple(new_rect), new_angle)

            # Redraw preview (debounced might be better here?)
            self.update_preview_safe() # Use safe direct update for responsiveness


        # --- Handle Selection Rectangle Drawing ---
        elif self.selection_start_coords and self.selection_rect_id:
            self.selection_current_coords = (canvas_x, canvas_y)
            x0_c, y0_c = self.selection_start_coords
            self.preview_canvas.coords(self.selection_rect_id, x0_c, y0_c, canvas_x, canvas_y)


    def on_mouse_release(self, event):
        """Handles mouse button release for interaction or selection finalization."""
        if not self.processed_image or self._pan_active: return

        # --- Finalize Interaction with Existing Element ---
        if self.edit_interaction_mode and self.selected_area_uuid:
            print(f"Ending interaction: Mode={self.edit_interaction_mode}, UUID={self.selected_area_uuid}")

            # Record the *final* state change for Undo/Redo
            final_rect, final_angle = self._get_area_rect_angle(self.selected_area_type, self.selected_area_uuid)
            # Check if state actually changed before recording
            if self.edit_orig_rect_on_drag_start != final_rect or self.edit_orig_angle_on_drag_start != final_angle:
                 self.record_transform_action(self.selected_area_type, self.selected_area_uuid,
                                            self.edit_orig_rect_on_drag_start, self.edit_orig_angle_on_drag_start,
                                            final_rect, final_angle)


            # Clear interaction mode, but keep selection active
            self.edit_interaction_mode = None
            self.edit_drag_mouse_start = None
            self.edit_orig_rect_on_drag_start = None # Clear temp storage
            # Keep selected_area_uuid and selected_area_type
            self.update_preview_safe() # Redraw final state

        # --- Finalize Selection for NEW Blur/Blackout ---
        elif self.selection_start_coords and self.selection_rect_id:
            x0_c, y0_c = self.selection_start_coords
            x1_c, y1_c = event.x, event.y
            self.selection_current_coords = (x1_c, y1_c) # Store final canvas pos

            canvas_rect = (min(x0_c, x1_c), min(y0_c, y1_c), max(x0_c, x1_c), max(y0_c, y1_c))

            # Min selection size on canvas (e.g., 5 pixels)
            if canvas_rect[2] - canvas_rect[0] < 5 or canvas_rect[3] - canvas_rect[1] < 5:
                self.current_selection_original = None
                self.clear_selection_rectangle()
                print("Selection too small.")
                self.update_widget_states() # Ensure Add buttons are disabled
            else:
                # Convert canvas rectangle corners to original image coordinates
                orig_x0, orig_y0 = self.canvas_to_original_coords(canvas_rect[0], canvas_rect[1])
                orig_x1, orig_y1 = self.canvas_to_original_coords(canvas_rect[2], canvas_rect[3])

                if all(coord is not None for coord in [orig_x0, orig_y0, orig_x1, orig_y1]) \
                   and abs(orig_x1 - orig_x0) >= 1 and abs(orig_y1 - orig_y0) >= 1: # Min 1px in original coords
                    # Store selection rectangle in original coords (ensure x0<x1, y0<y1)
                    original_coords = (min(orig_x0, orig_x1), min(orig_y0, orig_y1),
                                       max(orig_x0, orig_x1), max(orig_y0, orig_y1))
                    selected_shape = self.edit_shape.get()
                    strength = self.blur_strength.get() # Needed for potential blur add

                    self.current_selection_original = (selected_shape, original_coords, strength)
                    print(f"Selection made (original coords): Shape={selected_shape}, Coords={original_coords}")
                    # Update rect appearance and enable Add buttons
                    self.preview_canvas.itemconfig(self.selection_rect_id, outline="lime", width=2, dash=())
                    self.update_widget_states() # Enable Add buttons
                else:
                    self.current_selection_original = None
                    self.clear_selection_rectangle()
                    print("Invalid selection area (check bounds or conversion).")
                    self.update_widget_states() # Disable Add buttons


        # Reset drag start state for selection drawing
        self.selection_start_coords = None
        # Don't reset other interaction states here, they are reset on press or action


    def _reset_interaction_states(self):
        """ Clears selection and interaction states """
        self.clear_selection_rectangle()
        self.current_selection_original = None
        self.selected_area_type = None
        self.selected_area_uuid = None
        self.edit_interaction_mode = None
        self.edit_drag_start_coords = None
        self.edit_drag_mouse_start = None
        self.edit_orig_rect_on_drag_start = None
        self.edit_orig_angle_on_drag_start = 0.0
        self.edit_center_on_drag_start = (0,0)
        self.edit_mouse_start_angle_on_drag = 0.0
        self._update_overlay_ui_for_selection() # Update listbox/scale
        self.update_widget_states()


    def _get_area_rect_angle(self, area_type, area_uuid):
        """ Gets the current rect (original coords) and angle for a given area """
        if not self.current_image_path: return None, 0.0
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return None, 0.0

        if area_type == 'blur':
            area = next((a for a in settings.get('blur_areas', []) if a['uuid'] == area_uuid), None)
            return (area['coords'], 0.0) if area else (None, 0.0)
        elif area_type == 'blackout':
            area = next((a for a in settings.get('blackout_areas', []) if a['uuid'] == area_uuid), None)
            return (area['coords'], 0.0) if area else (None, 0.0)
        elif area_type == 'wm' and area_uuid == 'main_wm':
            wm_info = self.wm_img_info # Use global watermark info
            # Ensure rect exists before returning
            return (wm_info.get('rect'), wm_info.get('angle', 0.0)) if wm_info and wm_info.get('rect') else (None, 0.0)
        elif area_type == 'overlay':
            overlay = next((o for o in settings.get('overlays', []) if o['uuid'] == area_uuid), None)
            # Ensure rect exists
            return (overlay.get('rect'), overlay.get('angle', 0.0)) if overlay and overlay.get('rect') else (None, 0.0)

        return None, 0.0


    def _update_area_state(self, area_type, area_uuid, new_rect, new_angle):
         """ Updates the state (rect/angle) of an area in image_settings """
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return
         updated = False

         # Ensure rect coords are integers
         new_rect = tuple(map(int, new_rect)) if new_rect else None

         if area_type == 'blur':
             for area in settings.get('blur_areas', []):
                  if area['uuid'] == area_uuid: area['coords'] = new_rect; updated = True; break
         elif area_type == 'blackout':
              for area in settings.get('blackout_areas', []):
                   if area['uuid'] == area_uuid: area['coords'] = new_rect; updated = True; break
         elif area_type == 'wm' and area_uuid == 'main_wm':
              # Update the global watermark info directly
              if self.wm_img_info: # Check if global info exists
                   self.wm_img_info['rect'] = new_rect
                   self.wm_img_info['angle'] = new_angle
                   updated = True
         elif area_type == 'overlay':
             for overlay in settings.get('overlays', []):
                  if overlay['uuid'] == area_uuid:
                       overlay['rect'] = new_rect
                       overlay['angle'] = new_angle
                       updated = True
                       break
         # if updated:
         #      print(f"Updated state for {area_type} {area_uuid}: Rect={new_rect}, Angle={new_angle:.1f}")


    # --- Manual Edits (Blur/Blackout) Management ---

    def add_edit_area_action(self, blur=True):
        """Adds the currently selected area (blur/blackout) - UNDOABLE ACTION."""
        if not self.current_selection_original or not self.current_image_path:
            messagebox.showwarning("No Selection", "Draw a rectangle or circle on the preview first.")
            return

        shape, coords_orig, strength = self.current_selection_original
        area_uuid = str(uuid.uuid4())
        area_data = {'uuid': area_uuid, 'shape': shape, 'coords': coords_orig}
        area_type = 'blur' if blur else 'blackout'
        if blur: area_data['strength'] = strength

        # --- Undo/Redo Logic ---
        action = {
            'type': 'add_edit_area',
            'area_type': area_type,
            'area_uuid': area_uuid,
            'area_data': area_data # Store data needed to remove it
        }
        self._add_undo_action(action)
        # ---

        self._add_edit_area_internal(area_type, area_data)
        self.status_label.config(text=f"{area_type.capitalize()} area ({shape}) added.")

        # Clear the temporary selection visual and state
        self._reset_interaction_states() # Resets selection + interaction
        self.update_preview_safe()
        self.update_widget_states() # Update clear/remove button states

    def _add_edit_area_internal(self, area_type, area_data):
        """Internal logic to add area data to image_settings."""
        if not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return

        list_name = area_type + '_areas'
        if list_name not in settings: settings[list_name] = []
        settings[list_name].append(area_data)


    def remove_selected_area_action(self):
         """Removes the selected blur or blackout area - UNDOABLE ACTION."""
         if not self.selected_area_uuid or self.selected_area_type not in ['blur', 'blackout']:
             messagebox.showwarning("No Selection", "Click on a blur or blackout area outline first.")
             return
         if not self.current_image_path: return

         area_type = self.selected_area_type
         area_uuid = self.selected_area_uuid
         area_data = self._get_edit_area_data(area_type, area_uuid) # Get data before removing

         if area_data:
              # --- Undo/Redo Logic ---
              action = {
                   'type': 'remove_edit_area',
                   'area_type': area_type,
                   'area_uuid': area_uuid,
                   'area_data': area_data # Store data needed to re-add it
              }
              self._add_undo_action(action)
              # ---

              if self._remove_edit_area_internal(area_type, area_uuid):
                  self.status_label.config(text=f"Removed {area_type} area.")
                  self._reset_interaction_states() # Deselect
                  self.update_preview_safe()
                  self.update_widget_states()
              else:
                   print(f"Internal remove failed for {area_type} area {area_uuid}.")
         else:
              print(f"Could not find {area_type} area {area_uuid} to remove.")


    def _remove_edit_area_internal(self, area_type, area_uuid):
        """Internal logic to remove area data from image_settings."""
        if not self.current_image_path: return False
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return False

        list_name = area_type + '_areas'
        target_list = settings.get(list_name, [])
        initial_len = len(target_list)
        settings[list_name] = [area for area in target_list if area['uuid'] != area_uuid]
        return len(settings[list_name]) < initial_len


    def _get_edit_area_data(self, area_type, area_uuid):
         """Gets the data dict for a specific blur/blackout area."""
         if not self.current_image_path: return None
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return None
         target_list = settings.get(area_type + '_areas', [])
         return next((area for area in target_list if area['uuid'] == area_uuid), None)


    def clear_manual_areas_action(self):
        """Clears all blur and blackout areas - UNDOABLE ACTION."""
        if not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return
        blur_areas = settings.get('blur_areas', [])
        blackout_areas = settings.get('blackout_areas', [])
        if not blur_areas and not blackout_areas: return # Nothing to clear

        # --- Undo/Redo Logic ---
        action = {
            'type': 'clear_edit_areas',
            'removed_blurs': list(blur_areas), # Store copies of removed lists
            'removed_blackouts': list(blackout_areas)
        }
        self._add_undo_action(action)
        # ---

        self._clear_manual_areas_internal()
        self.status_label.config(text="Manual edit areas cleared.")
        self._reset_interaction_states() # Deselect everything
        self.update_preview_safe()
        self.update_widget_states()

    def _clear_manual_areas_internal(self):
         """Internal logic to clear area lists in image_settings."""
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return
         settings['blur_areas'] = []
         settings['blackout_areas'] = []


    def clear_selection_rectangle(self):
        """Deletes the temporary yellow selection rectangle from the canvas."""
        if self.selection_rect_id:
            try: self.preview_canvas.delete(self.selection_rect_id)
            except tk.TclError: pass
            self.selection_rect_id = None
        # Don't disable buttons here, release does that check


    # --- Watermark (Text/Image) & Overlay Actions ---

    def record_text_wm_change(self, event=None):
         """Records change in text watermark settings for undo."""
         # This needs to capture the *previous* state relative to the change.
         # This simplified version updates preview but doesn't add to undo stack easily.
         # A full implementation would need to know *which* setting changed
         # and store its previous value in the undo action.
         # For now, just update preview. Full undo for text WM is complex.
         self.update_preview_debounced()

    def choose_watermark_color_action(self):
         """Opens color chooser and records change for undo."""
         if not self.current_image_path: return # Need context, though text WM is global
         current_color = self.watermark_color.get()
         try:
             color_code = colorchooser.askcolor(title="Choose Watermark Color", initialcolor=current_color)
             if color_code and color_code[1] and color_code[1] != current_color:
                 new_color = color_code[1]
                 # --- Undo/Redo Logic ---
                 action = {
                      'type': 'change_text_wm_setting',
                      'setting': 'color',
                      'old_value': current_color,
                      'new_value': new_color
                 }
                 self._add_undo_action(action)
                 # ---
                 self.watermark_color.set(new_color)
                 self.update_preview_safe()
         except Exception as e:
             messagebox.showerror("Color Chooser Error", f"Failed to open color chooser:\n{e}")

    def toggle_image_wm_action(self):
         """Handles enabling/disabling the main image watermark - UNDOABLE (sort of)."""
         # Enabling/disabling visibility isn't easily undone with state snapshots.
         # We link enable state to path existence now. Use Browse/Remove instead.
         # This just updates preview based on checkbox.
         # Watermark is global, no need for current_image_path or settings

         # If enabling, but no path exists, show warning.
         # Check the global watermark info
         # Removed check that prevented enabling before browsing.
         # Checkbox state now directly reflects user intent.

         # Update preview and widget states to reflect the change
         self.update_widget_states() # Ensure controls enable/disable correctly
         self.update_preview_safe()
         self.update_widget_states()


    def browse_watermark_image_action(self):
        """Browses for main image watermark - UNDOABLE ACTION."""
        # Watermark is now global, no need for current_image_path or settings here.

        # Get the current global watermark info for potential replacement (though undo is removed for now)
        current_wm_info = self.wm_img_info
        # old_wm_info_copy = current_wm_info.copy() # Copy for undo (REMOVED FOR NOW)
        # old_wm_info_copy['pil_image'] = None # Don't store image in undo (REMOVED FOR NOW)

        filetypes = [('Image Files', '*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.webp'), ('All Files', '*.*')]
        try:
            filepath = filedialog.askopenfilename(title="Select Watermark Image", filetypes=filetypes)
            if filepath and self._is_image_file(filepath):
                self._set_global_watermark(filepath) # Call helper function
            elif filepath: # Handle case where a non-image file was selected
                 messagebox.showwarning("Invalid File", f"Selected file is not a recognized image format:\n{filepath}")

        except Exception as e:
            messagebox.showerror("File Browse Error", f"Error selecting watermark image:\n{e}")

    def _set_global_watermark(self, filepath):
        """Loads the image at filepath and sets it as the global watermark. Returns True on success."""
        if not os.access(filepath, os.R_OK):
            messagebox.showwarning("Permissions Error", f"Cannot read watermark file:\n{filepath}")
            return False
        try:
            wm_img = Image.open(filepath).convert("RGBA")
            # Create a temporary dict for the new info
            # Preserve existing opacity if available, otherwise default
            current_opacity = self.wm_img_info.get('opacity', 128)
            new_wm_info = {'path': None, 'pil_image': None, 'rect': None, 'angle': 0.0, 'opacity': current_opacity}
            new_wm_info['path'] = filepath
            new_wm_info['pil_image'] = wm_img
            # Reset placement for new image
            self._reset_object_placement(new_wm_info) # Pass dict to modify

            # Update the GLOBAL watermark info directly
            self.wm_img_info.update(new_wm_info)

            # --- Undo/Redo Logic (REMOVED FOR NOW for global change) ---
            # action = { ... }
            # self._add_undo_action(action)
            # ---
            self.use_image_watermark.set(True) # Enable checkbox
            self._apply_loaded_settings_to_ui() # Update UI entry/opacity
            self.status_label.config(text="Watermark image loaded.")
            self._reset_interaction_states() # Deselect any previous area
            self.update_widget_states() # Update button states etc.
            self.update_preview_safe()
            return True # Indicate success
        except UnidentifiedImageError:
            messagebox.showerror("Load Error", f"Cannot identify watermark image file:\n{filepath}")
            return False
        except Exception as e:
            messagebox.showerror("Load Error", f"Failed to load watermark image:\n{e}")
            import traceback
            traceback.print_exc()
            return False


    def reset_image_wm_placement_action(self):
        """Resets placement of the current image's watermark - UNDOABLE ACTION."""
        # Watermark is global, operate on self.wm_img_info
        wm_info = self.wm_img_info
        if not wm_info or not wm_info.get('path') or not wm_info.get('rect'):
             messagebox.showinfo("Reset Watermark", "No watermark image loaded or placed yet.")
             return # Nothing to reset

        # Reset placement directly on the global wm_info
        self._reset_object_placement(wm_info) # Modifies wm_info in place

        # --- Undo/Redo Logic (REMOVED FOR NOW for global change) ---
        # if new_rect != old_rect or new_angle != old_angle:
        #      action = { ... }
        #      self._add_undo_action(action)
        # ---

        # Update UI and preview
        self._apply_loaded_settings_to_ui() # Update UI elements if needed
        self.status_label.config(text="Watermark placement reset.")
        self._reset_interaction_states() # Deselect
        self.update_preview_safe()

        # Apply reset to actual settings
        self._reset_object_placement(wm_info) # Reset original dict
        self.update_preview_safe()
        self.status_label.config(text="Watermark placement reset.")


    def record_image_wm_change(self, event=None):
         """Records opacity change for image watermark for undo."""
         # Watermark is global, operate on self.wm_img_info
         wm_info = self.wm_img_info
         if not wm_info or not wm_info.get('path'): return # No WM active

         new_opacity = self.watermark_image_opacity.get()
         old_opacity = wm_info.get('opacity', 128)

         if new_opacity != old_opacity:
              # --- Undo/Redo Logic (REMOVED FOR NOW for global change) ---
              # action = { ... }
              # self._add_undo_action(action)
              # ---
              wm_info['opacity'] = new_opacity # Update global setting
              self.update_preview_safe() # Update preview (opacity applied in rendering)


    # --- Overlay Management ---

    def _update_overlay_listbox(self):
        """Updates the overlay listbox based on current image settings."""
        self.overlay_listbox.delete(0, tk.END)
        if not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return

        overlays = settings.get('overlays', [])
        for i, overlay in enumerate(overlays):
            path = overlay.get('path')
            name = os.path.basename(path) if path else f"Overlay {i+1} (No Path)"
            display_name = f"{i+1}: {name[:25]}{'...' if len(name)>25 else ''}"
            self.overlay_listbox.insert(tk.END, display_name)
            # Store UUID with the item text? No, retrieve by index later.

    def _get_overlay_uuid_from_listbox_index(self, index):
         """ Get overlay UUID based on its index in the listbox/settings list """
         if not self.current_image_path: return None
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return None
         overlays = settings.get('overlays', [])
         if 0 <= index < len(overlays):
              return overlays[index]['uuid']
         return None

    def _get_listbox_index_from_overlay_uuid(self, uuid_to_find):
         """ Get listbox index based on overlay UUID """
         if not self.current_image_path: return -1
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return -1
         overlays = settings.get('overlays', [])
         for i, overlay in enumerate(overlays):
             if overlay['uuid'] == uuid_to_find:
                 return i
         return -1

    def on_overlay_select(self, event=None):
         """Handles selection change in the overlay listbox."""
         selected_indices = self.overlay_listbox.curselection()
         if not selected_indices:
              self.selected_overlay_uuid = None
              # If the currently selected area *was* an overlay, deselect it
              if self.selected_area_type == 'overlay':
                   self._reset_interaction_states()
         else:
              selected_index = selected_indices[0]
              self.selected_overlay_uuid = self._get_overlay_uuid_from_listbox_index(selected_index)
              # If selected, deselect WM or edits
              if self.selected_overlay_uuid:
                   self.selected_area_uuid = self.selected_overlay_uuid
                   self.selected_area_type = 'overlay'
              # No need for an else here, covered by the check at the start

         self._update_overlay_ui_for_selection() # Update opacity scale
         self.update_widget_states() # Update button states
         self.update_preview_safe() # Redraw handles for selected overlay


    def _update_overlay_ui_for_selection(self):
         """ Updates overlay controls (opacity scale) based on selection. """
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return

         if self.selected_area_type == 'overlay' and self.selected_area_uuid:
              overlay = next((o for o in settings['overlays'] if o['uuid'] == self.selected_area_uuid), None)
              if overlay:
                   self.overlay_opacity_var.set(overlay.get('opacity', 128))
                   self.overlay_opacity_scale.config(state=tk.NORMAL)
                   return

         # If no overlay selected or not found, disable scale and reset var
         self.overlay_opacity_var.set(128)
         self.overlay_opacity_scale.config(state=tk.DISABLED)


    def add_overlay_action(self):
         """Browses for and adds a new image overlay - UNDOABLE ACTION."""
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return

         filetypes = [('Image Files', '*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.webp'), ('All Files', '*.*')]
         try:
             filepath = filedialog.askopenfilename(title="Select Overlay Image", filetypes=filetypes)
             if filepath and self._is_image_file(filepath):
                 self._add_overlay_from_path(filepath) # Call helper function
             elif filepath: # Handle case where a non-image file was selected
                  messagebox.showwarning("Invalid File", f"Selected file is not a recognized image format:\n{filepath}")

         except Exception as e:
              messagebox.showerror("File Browse Error", f"Error selecting overlay image:\n{e}")

    def _add_overlay_from_path(self, filepath):
        """Loads an image from filepath and adds it as an overlay. Returns True on success."""
        if not self.current_image_path: return False # Need a base image
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return False

        if not os.access(filepath, os.R_OK):
            messagebox.showerror("File Access Error", f"Cannot read overlay image file:\n{filepath}")
            return False
        try:
            overlay_img = Image.open(filepath).convert("RGBA")
            overlay_uuid = str(uuid.uuid4())
            new_overlay = {
                 'uuid': overlay_uuid,
                 'path': filepath,
                 'pil_image': overlay_img,
                 'rect': None, # Initial placeholder
                 'angle': 0.0,
                 'opacity': 128 # Default opacity
            }
            # Reset placement based on the main image dimensions at the time of adding
            # Use processed_base_size if available, otherwise original_image size
            base_img_for_placement = Image.new('RGB', self.processed_base_size or self.original_image.size)
            self._reset_object_placement(new_overlay) # Reset placement for the new overlay dict

            # Prepare copy for undo stack
            new_overlay_copy = new_overlay.copy()
            new_overlay_copy['pil_image'] = None

            # --- Undo/Redo Logic ---
            action = {
                 'type': 'add_overlay',
                 'overlay_data': new_overlay_copy # Store data needed to remove it (no image)
            }
            self._add_undo_action(action)
            # ---

            self._add_overlay_internal(new_overlay) # Add to settings (with image)
            self._update_overlay_listbox() # Update UI list
            # Select the newly added overlay
            new_index = self._get_listbox_index_from_overlay_uuid(overlay_uuid)
            if new_index != -1:
                 self.overlay_listbox.selection_clear(0, tk.END)
                 self.overlay_listbox.selection_set(new_index)
                 self.on_overlay_select() # Trigger selection logic

            self.status_label.config(text=f"Overlay added: {os.path.basename(filepath)}")
            self.update_preview_safe() # Show the new overlay
            self.update_widget_states()
            return True # Indicate success

        except UnidentifiedImageError:
            messagebox.showerror("Load Error", f"Cannot identify overlay image file:\n{filepath}")
            return False
        except Exception as load_e:
             # Print exact exception type and message for debugging
             print(f"Caught Exception Type: {type(load_e)}")
             print(f"Caught Exception Message: {load_e}")
             messagebox.showerror("Overlay Load Error", f"Failed to load overlay image:\n{type(load_e).__name__}: {load_e}")
             import traceback
             traceback.print_exc()
             return False

# Removed duplicate definition of _add_overlay_from_path which contained the TypeError

    def _add_overlay_internal(self, overlay_data):
        """Internal: Adds overlay dict to settings."""
        if not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return
        if 'overlays' not in settings: settings['overlays'] = []
        settings['overlays'].append(overlay_data)


    def remove_selected_overlay_action(self):
         """Removes the selected overlay - UNDOABLE ACTION."""
         if not self.selected_overlay_uuid or not self.current_image_path:
             messagebox.showwarning("No Selection", "Select an overlay from the list first.")
             return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return

         overlay_uuid = self.selected_overlay_uuid
         current_index = self._get_listbox_index_from_overlay_uuid(overlay_uuid)
         if current_index == -1: return # Not found

         overlays = settings.get('overlays', [])
         overlay_data = overlays[current_index].copy() # Get data before removing
         # Don't store PIL image in undo stack
         overlay_data['pil_image'] = None

         # --- Undo/Redo Logic ---
         action = {
              'type': 'remove_overlay',
              'overlay_data': overlay_data,
              'original_index': current_index # Store index to reinsert correctly
         }
         self._add_undo_action(action)
         # ---

         if self._remove_overlay_internal(overlay_uuid):
             self.status_label.config(text="Overlay removed.")
             self._reset_interaction_states() # Clear selection etc.
             self._update_overlay_listbox() # Update UI list
             self.update_preview_safe()
             self.update_widget_states()


    def _remove_overlay_internal(self, overlay_uuid):
        """Internal: Removes overlay dict from settings."""
        if not self.current_image_path: return False
        settings = self.image_settings.get(self.current_image_path)
        if not settings or 'overlays' not in settings: return False
        initial_len = len(settings['overlays'])
        settings['overlays'] = [o for o in settings['overlays'] if o['uuid'] != overlay_uuid]
        return len(settings['overlays']) < initial_len


    def change_overlay_order_action(self, direction):
         """Moves the selected overlay up or down - UNDOABLE ACTION."""
         if not self.selected_overlay_uuid or not self.current_image_path:
              messagebox.showwarning("No Selection", "Select an overlay from the list first.")
              return
         settings = self.image_settings.get(self.current_image_path)
         if not settings or 'overlays' not in settings: return

         uuid_to_move = self.selected_overlay_uuid
         overlays = settings['overlays']
         current_index = self._get_listbox_index_from_overlay_uuid(uuid_to_move)
         if current_index == -1: return # Should not happen if selected

         new_index = current_index
         if direction == "up" and current_index > 0:
              new_index = current_index - 1
         elif direction == "down" and current_index < len(overlays) - 1:
              new_index = current_index + 1

         if new_index != current_index:
              # --- Undo/Redo Logic ---
              action = {
                   'type': 'reorder_overlay',
                   'overlay_uuid': uuid_to_move,
                   'old_index': current_index,
                   'new_index': new_index
              }
              self._add_undo_action(action)
              # ---

              self._change_overlay_order_internal(uuid_to_move, new_index) # Apply the change
              self._update_overlay_listbox() # Update UI list

              # Re-select the moved item
              self.overlay_listbox.selection_clear(0, tk.END)
              self.overlay_listbox.selection_set(new_index)
              # self.selected_overlay_uuid remains the same
              self.on_overlay_select() # Update state based on selection

              self.update_preview_safe() # Order affects rendering
              self.status_label.config(f"Overlay order changed.")


    def _change_overlay_order_internal(self, overlay_uuid, new_index):
         """Internal: Changes overlay order in settings."""
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings or 'overlays' not in settings: return
         overlays = settings['overlays']
         item = next((o for o in overlays if o['uuid'] == overlay_uuid), None)
         if item:
              overlays.remove(item)
              overlays.insert(new_index, item)


    def record_overlay_opacity_change(self, event=None):
         """Records change in selected overlay opacity for undo."""
         if not self.selected_overlay_uuid or not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return

         overlay = next((o for o in settings['overlays'] if o['uuid'] == self.selected_overlay_uuid), None)
         if not overlay: return

         new_opacity = self.overlay_opacity_var.get()
         old_opacity = overlay.get('opacity', 128)

         if new_opacity != old_opacity:
              # --- Undo/Redo Logic ---
              action = {
                   'type': 'change_opacity',
                   'area_type': 'overlay',
                   'area_uuid': self.selected_overlay_uuid,
                   'old_value': old_opacity,
                   'new_value': new_opacity
              }
              self._add_undo_action(action)
              # ---
              overlay['opacity'] = new_opacity # Update setting
              self.update_preview_safe() # Update rendering


    def _reset_object_placement(self, object_info_dict):
         """Sets initial size/position for WM or overlay dict *in place*."""
         if not self.original_image or 'pil_image' not in object_info_dict or not object_info_dict['pil_image']:
              object_info_dict['rect'] = None
              object_info_dict['angle'] = 0.0
              return

         main_w, main_h = self.original_image.size
         obj_w, obj_h = object_info_dict['pil_image'].size

         # Initial size: e.g., 20% of main image width, maintain aspect ratio
         if obj_w > 0 and obj_h > 0:
             target_obj_w = main_w * 0.2
             aspect = obj_h / obj_w
             target_obj_h = target_obj_w * aspect
         else: # Handle zero-size image?
              target_obj_w, target_obj_h = 50, 50 # Default size

         # Clamp max size? No, let user resize down if needed.

         center_x = main_w / 2
         center_y = main_h / 2

         x0 = center_x - target_obj_w / 2
         y0 = center_y - target_obj_h / 2
         x1 = center_x + target_obj_w / 2
         y1 = center_y + target_obj_h / 2

         object_info_dict['rect'] = tuple(map(int, (x0, y0, x1, y1)))
         object_info_dict['angle'] = 0.0

    # --- Adjustments ---

    def reset_adjustments_action(self):
        """Resets adjustment sliders and updates preview - UNDOABLE ACTION."""
        if not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return

        current_adjustments = settings.get('adjustments', {}).copy()
        if current_adjustments.get('brightness', 1.0) == 1.0 and \
           current_adjustments.get('contrast', 1.0) == 1.0 and \
           current_adjustments.get('saturation', 1.0) == 1.0:
            return # Already at defaults

        # --- Undo/Redo Logic ---
        action = {
            'type': 'change_adjustments',
            'old_values': current_adjustments,
            'new_values': {'brightness': 1.0, 'contrast': 1.0, 'saturation': 1.0}
        }
        self._add_undo_action(action)
        # ---

        self._apply_adjustments_internal(1.0, 1.0, 1.0) # Apply reset internally
        self.brightness_var.set(1.0)
        self.contrast_var.set(1.0)
        self.saturation_var.set(1.0)
        self.update_preview_safe()
        self.status_label.config(text="Adjustments reset.")


    def record_adjustment_change(self, event=None):
        """Records the final adjustment values after slider release for undo."""
        if not self.current_image_path: return
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return

        old_adjustments = settings.get('adjustments', {'brightness': 1.0, 'contrast': 1.0, 'saturation': 1.0}).copy()
        new_adjustments = {
            'brightness': self.brightness_var.get(),
            'contrast': self.contrast_var.get(),
            'saturation': self.saturation_var.get()
        }

        # Only record if values actually changed
        if old_adjustments != new_adjustments:
             # --- Undo/Redo Logic ---
             action = {
                 'type': 'change_adjustments',
                 'old_values': old_adjustments,
                 'new_values': new_adjustments
             }
             self._add_undo_action(action)
             # ---
             self._apply_adjustments_internal(new_adjustments['brightness'], new_adjustments['contrast'], new_adjustments['saturation'])
             # Preview is already updated by slider command, no need to call update_preview_safe here
        else:
            # If values didn't change on release, still update internal state just in case
            self._apply_adjustments_internal(new_adjustments['brightness'], new_adjustments['contrast'], new_adjustments['saturation'])


    def _apply_adjustments_internal(self, brightness, contrast, saturation):
         """Internal logic to store adjustment values in settings."""
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return
         settings['adjustments'] = {'brightness': brightness, 'contrast': contrast, 'saturation': saturation}


    # --- Effect Application Helpers (used by Preview and Conversion) ---

    def apply_filter(self, img, filter_val):
        # Simplified version of apply_filter for background thread
        if filter_val == "None": return img
        try:
             original_mode = img.mode
             if filter_val == "Grayscale":
                  # Keep as L if possible unless original was RGBA/LA/P(with transparency)
                  if original_mode in ['RGBA', 'LA'] or (original_mode == 'P' and 'transparency' in img.info):
                       return img.convert("L").convert('RGBA')
                  else:
                       return img.convert("L")
             elif filter_val == "Sepia":
                  img_rgba = img if img.mode == 'RGBA' else img.convert('RGBA')
                  return self._apply_sepia_filter(img_rgba.copy()) # apply_sepia expects RGBA
             elif filter_val == "Blur": return img.filter(ImageFilter.GaussianBlur(radius=2))
             elif filter_val == "Sharpen": return img.filter(ImageFilter.SHARPEN)
             elif filter_val == "Edge Enhance": return img.filter(ImageFilter.EDGE_ENHANCE_MORE)
             elif filter_val == "Contour":
                  temp_img = img if img.mode == 'L' else img.convert('L')
                  filtered = temp_img.filter(ImageFilter.CONTOUR)
                  # Convert back to original mode if it wasn't L
                  return filtered.convert(original_mode) if original_mode != 'L' else filtered
             return img
        except Exception as e:
             print(f"Filter Error ({filter_val}): {e}")
             return img # Return original on error

    def _apply_sepia_filter(self, img):
        """Applies sepia tone. Assumes img is RGBA."""
        if img.mode != 'RGBA': img = img.convert('RGBA')
        width, height = img.size
        pixels = img.load()
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]
                tr = int(0.393 * r + 0.769 * g + 0.189 * b)
                tg = int(0.349 * r + 0.686 * g + 0.168 * b)
                tb = int(0.272 * r + 0.534 * g + 0.131 * b)
                pixels[x, y] = (min(255, tr), min(255, tg), min(255, tb), a)
        return img

    def apply_resize(self, img, preset_val, w_str, h_str):
        target_w, target_h = None, None
        if preset_val != "Custom":
             dims = { "YouTube Thumbnail (1280x720)": (1280, 720), "Facebook Post (1200x630)": (1200, 630),
                      "Instagram Post (1080x1080)": (1080, 1080), "Twitter Post (1024x512)": (1024, 512) }
             target_w, target_h = dims.get(preset_val, (None, None))
        else:
             try: target_w = int(w_str) if w_str else None
             except: pass
             try: target_h = int(h_str) if h_str else None
             except: pass

        if target_w or target_h:
             try:
                 current_w, current_h = img.size
                 if current_w <= 0 or current_h <= 0: return img # Cannot resize invalid image

                 # Calculate missing dimension while maintaining aspect ratio
                 if target_w and not target_h: target_h = int(current_h * (target_w / current_w))
                 elif target_h and not target_w: target_w = int(current_w * (target_h / current_h))

                 # Ensure dimensions are valid positive integers
                 target_w = max(1, target_w) if target_w is not None else None
                 target_h = max(1, target_h) if target_h is not None else None

                 if target_w and target_h:
                     if (target_w, target_h) != (current_w, current_h):
                          # print(f"Resizing from {current_w}x{current_h} to {target_w}x{target_h}")
                          img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
             except ZeroDivisionError:
                  print("Resize Error: Original dimension was zero.")
             except Exception as e:
                  print(f"Resize Error: {e}")
        return img

    def apply_adjustments(self, img, adj_settings):
        """Applies brightness, contrast, saturation."""
        brightness = adj_settings.get('brightness', 1.0)
        contrast = adj_settings.get('contrast', 1.0)
        saturation = adj_settings.get('saturation', 1.0)

        # Apply only if value is different from default 1.0
        try:
            if abs(brightness - 1.0) > 1e-6:
                enhancer = ImageEnhance.Brightness(img)
                img = enhancer.enhance(brightness)
            if abs(contrast - 1.0) > 1e-6:
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(contrast)
            if abs(saturation - 1.0) > 1e-6:
                enhancer = ImageEnhance.Color(img)
                img = enhancer.enhance(saturation)
        except Exception as e:
            print(f"Adjustment application error: {e}")
        return img

    def apply_manual_edits(self, img, blur_areas_list, blackout_areas_list):
        """Applies blur and blackout areas to the image (uses original coordinates)."""
        if not blur_areas_list and not blackout_areas_list: return img

        editable_img = img if img.mode == 'RGBA' else img.convert('RGBA')
        draw = ImageDraw.Draw(editable_img)
        current_w, current_h = editable_img.size
        if not self.original_image: return img # Need original size for scaling
        original_w, original_h = self.original_image.size # Use original for scaling source

        scale_x = current_w / original_w if original_w > 0 else 1
        scale_y = current_h / original_h if original_h > 0 else 1

        # Apply Blur Areas
        for area in blur_areas_list:
             shape, coords_orig, strength = area['shape'], area['coords'], area['strength']
             ox0, oy0, ox1, oy1 = coords_orig
             scaled_coords = (ox0 * scale_x, oy0 * scale_y, ox1 * scale_x, oy1 * scale_y)
             crop_region = (max(0, int(scaled_coords[0])), max(0, int(scaled_coords[1])),
                           min(current_w, int(scaled_coords[2])), min(current_h, int(scaled_coords[3])))

             if crop_region[2] > crop_region[0] and crop_region[3] > crop_region[1]:
                  try:
                      cropped = editable_img.crop(crop_region)
                      # Ensure blur radius isn't excessively large for small crops
                      effective_strength = min(strength, max(cropped.width, cropped.height)/4) if cropped.width > 0 and cropped.height > 0 else strength
                      blurred_cropped = cropped.filter(ImageFilter.GaussianBlur(radius=effective_strength))

                      if shape == 'rectangle':
                          editable_img.paste(blurred_cropped, crop_region)
                      elif shape == 'circle':
                          mask = Image.new('L', cropped.size, 0)
                          mask_draw = ImageDraw.Draw(mask)
                          mask_draw.ellipse((0, 0, cropped.width-1, cropped.height-1), fill=255) # Use -1 for bounds
                          editable_img.paste(blurred_cropped, crop_region, mask)
                          del mask_draw
                  except Exception as e: print(f"Blur application error: {e}")

        # Apply Blackout Areas
        for area in blackout_areas_list:
             shape, coords_orig = area['shape'], area['coords']
             ox0, oy0, ox1, oy1 = coords_orig
             scaled_coords = [max(0, int(ox0 * scale_x)), max(0, int(oy0 * scale_y)),
                             min(current_w, int(ox1 * scale_x)), min(current_h, int(oy1 * scale_y))]

             if scaled_coords[2] > scaled_coords[0] and scaled_coords[3] > scaled_coords[1]:
                  try:
                      if shape == 'rectangle': draw.rectangle(scaled_coords, fill="black")
                      elif shape == 'circle': draw.ellipse(scaled_coords, fill="black")
                  except Exception as e: print(f"Blackout application error: {e}")

        del draw
        return editable_img


    def apply_text_watermark(self, img):
        """Adds text watermark based on GLOBAL settings."""
        if img.mode != 'RGBA': img = img.convert('RGBA')
        try:
            text = self.watermark_text.get()
            if not text: return img
            size = self.watermark_font_size.get()
            color_hex = self.watermark_color.get()
            opacity = max(0, min(255, self.watermark_opacity.get()))
            position = self.watermark_position.get()
            r, g, b = tuple(int(color_hex.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
            color_rgba = (r, g, b, opacity)

            text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(text_layer)
            try: wm_font = ImageFont.truetype("arial.ttf", size)
            except: wm_font = ImageFont.load_default()

            try: bbox = draw.textbbox((0,0), text, font=wm_font, anchor='lt'); w=bbox[2]-bbox[0]; h=bbox[3]-bbox[1]
            except: ts = draw.textsize(text,font=wm_font); w=ts[0]; h=ts[1]

            if position == "Tile":
                sp_x = w + max(50, w*0.5); sp_y = h + max(30, h*0.5)
                if sp_x <=0: sp_x=100;
                if sp_y <=0: sp_y=50;
                y_tile = -(h//2)
                while y_tile < img.height:
                     x_tile = -(w//2)
                     while x_tile < img.width: draw.text((x_tile,y_tile), text, font=wm_font, fill=color_rgba, anchor='lt'); x_tile += sp_x
                     y_tile += sp_y
            elif position == "Diagonal Fit":
                 cx, cy = img.width/2, img.height/2
                 max_dim = int(math.sqrt(w**2 + h**2)) + 2
                 txt_img = Image.new('RGBA', (max_dim, max_dim), (0,0,0,0))
                 txt_draw = ImageDraw.Draw(txt_img)
                 txt_draw.text((max_dim/2, max_dim/2), text, font=wm_font, fill=color_rgba, anchor='mm')
                 angle = -math.degrees(math.atan2(img.height, img.width)) if img.width > 0 else 0
                 rot_txt = txt_img.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
                 rw, rh = rot_txt.size
                 px, py = int(cx - rw / 2), int(cy - rh / 2)
                 text_layer.paste(rot_txt, (px, py), rot_txt)
                 del txt_draw
            else: # Standard placement
                 x_pos, y_pos = self._get_watermark_position_coords(img.size, (w, h), position)
                 draw.text((x_pos, y_pos), text, font=wm_font, fill=color_rgba, anchor='lt')

            del draw
            return Image.alpha_composite(img, text_layer)
        except Exception as e:
             print(f"Text WM Error: {e}")
             return img


    def apply_single_image_overlay(self, img, overlay_info):
        """Applies a single image watermark or overlay based on its info dict."""
        if not overlay_info or not overlay_info.get('pil_image') or not overlay_info.get('rect'):
            # print(f"Skipping overlay application: Missing info {overlay_info}")
            return img # Cannot apply if missing image or placement
        if not self.original_image: return img # Need original size for scaling

        if img.mode != 'RGBA': img = img.convert('RGBA')

        try:
            wm_img_original = overlay_info['pil_image'].copy() # Work with a copy
            opacity = max(0, min(255, overlay_info.get('opacity', 128)))
            rect_orig = overlay_info['rect']
            angle = overlay_info.get('angle', 0.0)

            # Apply opacity
            if opacity < 255:
                 try:
                     alpha = wm_img_original.split()[3]
                     alpha = alpha.point(lambda p: int(p * (opacity / 255.0)))
                     wm_img_original.putalpha(alpha)
                 except IndexError: pass # No alpha channel

            # Scale rect_orig coordinates to the *current* image dimensions
            current_w, current_h = img.size
            original_w, original_h = self.original_image.size
            scale_x = current_w / original_w if original_w > 0 else 1
            scale_y = current_h / original_h if original_h > 0 else 1

            ox0, oy0, ox1, oy1 = rect_orig
            scaled_rect = (ox0 * scale_x, oy0 * scale_y, ox1 * scale_x, oy1 * scale_y)

            target_w = int(scaled_rect[2] - scaled_rect[0])
            target_h = int(scaled_rect[3] - scaled_rect[1])

            if target_w <= 0 or target_h <= 0: return img # Invalid size

            # Resize, Rotate, Paste
            wm_resized = wm_img_original.resize((target_w, target_h), Image.Resampling.LANCZOS)
            wm_rotated = wm_resized.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
            rot_w, rot_h = wm_rotated.size

            center_x = (scaled_rect[0] + scaled_rect[2]) / 2
            center_y = (scaled_rect[1] + scaled_rect[3]) / 2
            paste_x = int(center_x - rot_w / 2)
            paste_y = int(center_y - rot_h / 2)

            # Create layer and composite
            wm_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            wm_layer.paste(wm_rotated, (paste_x, paste_y), wm_rotated)
            return Image.alpha_composite(img, wm_layer)

        except Exception as e:
            path_hint = os.path.basename(overlay_info.get('path','Unknown')) if overlay_info else 'Unknown'
            print(f"Error applying image overlay/WM ({path_hint}): {e}")
            import traceback
            traceback.print_exc()
            return img # Return original on error

    def apply_overlays(self, img, overlays_list):
        """Applies all overlays from the list in order."""
        if not overlays_list: return img
        base_img = img if img.mode == 'RGBA' else img.convert('RGBA')
        for overlay_info in overlays_list: # Apply in list order (bottom to top)
             base_img = self.apply_single_image_overlay(base_img, overlay_info)
        return base_img

    def _get_watermark_position_coords(self, main_img_size, wm_size, position_name, padding=10):
        """Calculates top-left coords for standard watermark positions."""
        W, H = main_img_size
        w, h = wm_size
        pos = position_name.lower()

        if pos == "center": x, y = (W - w) // 2, (H - h) // 2
        elif pos == "top left": x, y = padding, padding
        elif pos == "top right": x, y = W - w - padding, padding
        elif pos == "bottom left": x, y = padding, H - h - padding
        elif pos == "bottom right": x, y = W - w - padding, H - h - padding
        else: x, y = (W - w) // 2, (H - h) // 2 # Default to center

        x = max(0, min(x, W - w))
        y = max(0, min(y, H - h))
        return int(x), int(y)


    # --- Zoom and Pan ---

    def on_mouse_wheel_zoom(self, event):
        if not self.processed_image: return

        canvas_x, canvas_y = event.x, event.y
        # Calculate mouse position relative to the image (in processed_image coords)
        proc_x_before, proc_y_before = self.get_processed_coords(canvas_x, canvas_y)
        if proc_x_before is None: return # Cannot determine image coords

        # Determine zoom direction and factor
        if sys.platform == "darwin": # MacOS convention
             delta = event.delta
             zoom_multiplier = 1.1 if delta > 0 else (1 / 1.1)
        else: # Windows/Linux convention
             delta = event.delta
             zoom_multiplier = 1.1 if delta > 0 else (1 / 1.1)

        new_zoom_factor = self.zoom_factor * zoom_multiplier
        # Clamp zoom factor (e.g., 10% to 3200%)
        new_zoom_factor = max(0.1, min(new_zoom_factor, 32.0))

        if abs(new_zoom_factor - self.zoom_factor) < 1e-6: return # No significant change

        # Calculate where the image point under the mouse *should* be on canvas *after* zoom
        canvas_x_target = proc_x_before * new_zoom_factor + self.pan_offset[0] # This uses old pan offset
        canvas_y_target = proc_y_before * new_zoom_factor + self.pan_offset[1]

        # Calculate the required change in pan offset to keep the point under the mouse
        delta_pan_x = canvas_x - canvas_x_target
        delta_pan_y = canvas_y - canvas_y_target

        # Update zoom and pan state
        self.zoom_factor = new_zoom_factor
        self.pan_offset[0] += delta_pan_x
        self.pan_offset[1] += delta_pan_y

        self._clamp_pan_offset() # Ensure image doesn't pan too far away
        self._update_zoom_label()
        self.update_preview_safe() # Redraw with new zoom/pan


    def on_pan_press(self, event):
        if not self.processed_image: return
        self.preview_canvas.config(cursor="fleur") # Change cursor
        self._pan_active = True
        self._pan_start_x = event.x
        self._pan_start_y = event.y

    def on_pan_drag(self, event):
        if not self._pan_active or not self.processed_image: return
        dx = event.x - self._pan_start_x
        dy = event.y - self._pan_start_y
        self.pan_offset[0] += dx
        self.pan_offset[1] += dy
        self._clamp_pan_offset() # Apply panning limits
        self._pan_start_x = event.x
        self._pan_start_y = event.y
        self.update_preview_safe() # Redraw with new pan

    def on_pan_release(self, event):
         if not self._pan_active: return
         self.preview_canvas.config(cursor="") # Restore cursor
         self._pan_active = False

    def zoom_in(self): self._zoom_step(1.25)
    def zoom_out(self): self._zoom_step(1 / 1.25)

    def _zoom_step(self, multiplier):
         """ Zooms by a fixed multiplier, centering on canvas center. """
         if not self.processed_image: return
         # Use canvas center as the zoom focus point
         canvas_center_x = self.preview_canvas.winfo_width() / 2
         canvas_center_y = self.preview_canvas.winfo_height() / 2
         # Create a mock event object
         mock_event = tk.Event()
         mock_event.x = int(canvas_center_x)
         mock_event.y = int(canvas_center_y)
         mock_event.delta = 1 if multiplier > 1 else -1 # Simulate direction
         # Call the wheel zoom handler with the mock event
         # Temporarily adjust multiplier calculation based on fixed step
         original_zoom = self.zoom_factor
         target_zoom = original_zoom * multiplier
         # Simulate delta based on desired direction for on_mouse_wheel_zoom logic
         mock_event.delta = 1 if target_zoom > original_zoom else -1
         # Need to ensure on_mouse_wheel_zoom uses the step multiplier
         # Let's simplify: directly set zoom and recalculate pan to center
         self._zoom_to(target_zoom, canvas_center_x, canvas_center_y)


    def _zoom_to(self, new_zoom_factor, focus_canvas_x, focus_canvas_y):
         """ Zooms to a specific factor, keeping focus point stationary. """
         if not self.processed_image: return

         proc_x_focus, proc_y_focus = self.get_processed_coords(focus_canvas_x, focus_canvas_y)
         if proc_x_focus is None: return # Cannot map focus point

         # Clamp zoom factor
         new_zoom_factor = max(0.1, min(new_zoom_factor, 32.0))
         if abs(new_zoom_factor - self.zoom_factor) < 1e-6: return # No change

         # Calculate new pan offset needed to keep focus point stationary
         # new_pan_x = focus_canvas_x - proc_x_focus * new_zoom_factor
         # new_pan_y = focus_canvas_y - proc_y_focus * new_zoom_factor
         # Simplified math: delta_pan = canvas_coords - new_canvas_coords
         new_canvas_x = proc_x_focus * new_zoom_factor + self.pan_offset[0] # Target canvas X using old pan
         new_canvas_y = proc_y_focus * new_zoom_factor + self.pan_offset[1] # Target canvas Y using old pan
         delta_pan_x = focus_canvas_x - new_canvas_x
         delta_pan_y = focus_canvas_y - new_canvas_y

         self.zoom_factor = new_zoom_factor
         self.pan_offset[0] += delta_pan_x
         self.pan_offset[1] += delta_pan_y

         self._clamp_pan_offset()
         self._update_zoom_label()
         self.update_preview_safe()


    def zoom_fit(self):
        if not self.processed_image: return
        img_w, img_h = self.processed_image.size
        # Ensure canvas dimensions are up-to-date
        self.preview_canvas.update_idletasks()
        canvas_w = self.preview_canvas.winfo_width()
        canvas_h = self.preview_canvas.winfo_height()
        if img_w <= 0 or img_h <= 0 or canvas_w <= 1 or canvas_h <= 1: return

        # Calculate scale to fit the entire image within the canvas (no padding)
        scale = min(canvas_w / img_w, canvas_h / img_h)

        # Apply a tiny buffer to prevent potential floating point issues causing scrollbars
        # (Optional, but can sometimes help if edges are *exactly* aligned)
        # scale *= 0.999

        self.zoom_factor = scale
        # Center the image
        zoomed_w = img_w * self.zoom_factor
        zoomed_h = img_h * self.zoom_factor
        # Calculate top-left corner offset for centering
        self.pan_offset[0] = (canvas_w - zoomed_w) / 2
        self.pan_offset[1] = (canvas_h - zoomed_h) / 2

        self._clamp_pan_offset() # Should be unnecessary after centering, but safe
        self._update_zoom_label()
        self.update_preview_safe()

    def zoom_100(self):
         if not self.processed_image: return
         canvas_center_x = self.preview_canvas.winfo_width() / 2
         canvas_center_y = self.preview_canvas.winfo_height() / 2
         self._zoom_to(1.0, canvas_center_x, canvas_center_y)


    def _update_zoom_label(self):
        if hasattr(self, 'zoom_label'):
             self.zoom_label.config(text=f"Zoom: {self.zoom_factor:.1%}")

    def _clamp_pan_offset(self):
         """ Prevents panning the image completely out of view. """
         if not self.processed_image: return
         img_w, img_h = self.processed_image.size
         canvas_w = self.preview_canvas.winfo_width()
         canvas_h = self.preview_canvas.winfo_height()
         if canvas_w <= 1 or canvas_h <= 1: return # Avoid clamping if canvas size unknown

         zoomed_w = img_w * self.zoom_factor
         zoomed_h = img_h * self.zoom_factor

         # Allow panning slightly beyond edges (e.g., half canvas width/height)
         max_x_offset = canvas_w * 0.8 # Max top-left X (most of image off left)
         min_x_offset = canvas_w * 0.2 - zoomed_w # Min top-left X (most of image off right)
         max_y_offset = canvas_h * 0.8
         min_y_offset = canvas_h * 0.2 - zoomed_h

         # Allow less panning if image is smaller than canvas
         if zoomed_w < canvas_w:
             min_x_offset = canvas_w * 0.1 # Allow some space on left
             max_x_offset = canvas_w * 0.9 - zoomed_w # Allow some space on right
         if zoomed_h < canvas_h:
              min_y_offset = canvas_h * 0.1
              max_y_offset = canvas_h * 0.9 - zoomed_h


         self.pan_offset[0] = max(min_x_offset, min(self.pan_offset[0], max_x_offset))
         self.pan_offset[1] = max(min_y_offset, min(self.pan_offset[1], max_y_offset))


    def on_canvas_resize_debounced(self, event=None):
        """Handles canvas resize events, redraws preview after a delay."""
        if self._canvas_resize_job:
            self.root.after_cancel(self._canvas_resize_job)
        self._canvas_resize_job = self.root.after(250, self._on_canvas_resize_action)

    def _on_canvas_resize_action(self):
         """ Actual action to perform after canvas resize debounce. """
         # Option 1: Zoom to fit (might be jarring if user was zoomed in)
         # self.zoom_fit()
         # Option 2: Just redraw with current zoom/pan (better UX)
         self._clamp_pan_offset() # Re-clamp pan based on new size
         self.update_preview_safe()


    # --- Undo/Redo System ---

    def _add_undo_action(self, action_dict):
        """Adds an action to the current image's undo stack."""
        if not self.current_image_path: return # Cannot undo if no image selected

        # Clear redo stack
        self.current_redo_stack.clear()
        # Add action to undo stack
        self.current_undo_stack.append(action_dict)
        # Update button states
        self.update_undo_redo_buttons()
        # print(f"Undo Added: {action_dict['type']}") # Debug

    def undo(self, event=None):
        """Performs the Undo action."""
        if not self.current_undo_stack: return # Nothing to undo

        action = self.current_undo_stack.pop()
        print(f"Undoing: {action['type']}") # Debug

        # Push the *inverse* or the current state onto the redo stack BEFORE applying undo
        # This requires calculating the state *before* applying the undo action.
        # For simplicity now, we'll reconstruct the redo action inside _apply_action.

        if self._apply_action(action, is_undo=True):
            # Add the reverse action to the redo stack (done within _apply_action)
            self.update_preview_safe() # Update preview
            self.update_undo_redo_buttons() # Update button states
            self._apply_loaded_settings_to_ui() # Ensure UI controls match state
            self.status_label.config(text=f"Undo: {action['type']}")
        else:
             # If applying failed, put action back on stack? Risky.
             print(f"Failed to apply undo action: {action}")
             self.current_undo_stack.append(action) # Re-add if failed?

        return "break" # Prevent event propagation (e.g., text widget undo)

    def redo(self, event=None):
        """Performs the Redo action."""
        if not self.current_redo_stack: return # Nothing to redo

        action = self.current_redo_stack.pop()
        print(f"Redoing: {action['type']}") # Debug

        if self._apply_action(action, is_undo=False):
             # Add the original action back to the undo stack (done within _apply_action)
             self.update_preview_safe()
             self.update_undo_redo_buttons()
             self._apply_loaded_settings_to_ui() # Ensure UI controls match state
             self.status_label.config(text=f"Redo: {action['type']}")
        else:
             print(f"Failed to apply redo action: {action}")
             self.current_redo_stack.append(action) # Re-add if failed?

        return "break" # Prevent event propagation

    def _apply_action(self, action, is_undo):
        """Applies an action dictionary (for Undo or Redo). Returns True if successful."""
        action_type = action['type']
        settings = self.image_settings.get(self.current_image_path)
        if not settings: return False

        try:
            # --- Determine target state based on undo/redo ---
            # Most actions store 'old' and 'new' states/values
            target_prefix = 'old_' if is_undo else 'new_'
            source_prefix = 'new_' if is_undo else 'old_' # For constructing inverse action

            # --- Apply Specific Action Types ---
            if action_type == 'transform': # Covers rotate/flip, WM/Overlay move/resize/rotate
                area_type = action['area_type']
                area_uuid = action.get('area_uuid') # UUID for WM/Overlay/Edit
                target_rect = action.get(target_prefix + 'rect') # Use .get for flexibility
                target_angle = action.get(target_prefix + 'angle') # Use .get for flexibility

                if area_type == 'image': # Original image rotate/flip
                    target_rot = action.get(target_prefix + 'rotation', 0)
                    target_flip_h = action.get(target_prefix + 'flip_h', False)
                    target_flip_v = action.get(target_prefix + 'flip_v', False)
                    settings['rotation'] = target_rot
                    settings['flip_h'] = target_flip_h
                    settings['flip_v'] = target_flip_v
                    self._apply_image_transforms_from_settings() # Update self.rotated_flipped_image
                elif area_uuid: # WM or Overlay or Edit Area
                    self._update_area_state(area_type, area_uuid, target_rect, target_angle)

            elif action_type == 'filter':
                 target_filter = action[target_prefix + 'value']
                 settings['filter'] = target_filter # Update internal setting first
                 self.filter_var.set(target_filter) # Update UI

            elif action_type == 'change_adjustments': # Corrected type name
                 target_values = action[target_prefix + 'values']
                 self._apply_adjustments_internal(target_values['brightness'], target_values['contrast'], target_values['saturation'])
                 # Update UI vars
                 self.brightness_var.set(target_values['brightness'])
                 self.contrast_var.set(target_values['contrast'])
                 self.saturation_var.set(target_values['saturation'])

            elif action_type == 'add_edit_area':
                 area_type = action['area_type']
                 area_uuid = action['area_uuid']
                 if is_undo: # Undo Add = Remove
                      self._remove_edit_area_internal(area_type, area_uuid)
                 else: # Redo Add = Add
                      self._add_edit_area_internal(area_type, action['area_data'])

            elif action_type == 'remove_edit_area':
                 area_type = action['area_type']
                 area_uuid = action['area_uuid']
                 if is_undo: # Undo Remove = Add back
                      self._add_edit_area_internal(area_type, action['area_data'])
                 else: # Redo Remove = Remove again
                      self._remove_edit_area_internal(area_type, area_uuid)

            elif action_type == 'clear_edit_areas':
                 if is_undo: # Undo Clear = Restore
                      settings['blur_areas'] = list(action['removed_blurs'])
                      settings['blackout_areas'] = list(action['removed_blackouts'])
                 else: # Redo Clear = Clear again
                      self._clear_manual_areas_internal()

            elif action_type == 'change_text_wm_setting':
                 setting_name = action['setting']
                 target_value = action[target_prefix + 'value']
                 if setting_name == 'color': self.watermark_color.set(target_value)
                 # Add other text WM settings if made undoable

            elif action_type == 'change_image_wm':
                 target_info = action[target_prefix + 'info'].copy()
                 # Reload PIL image if path exists
                 target_info = self._reload_wm_image(target_info)
                 settings['wm_img_info'] = target_info
                 self.use_image_watermark.set(bool(target_info.get('path')))
                 # We need _apply_loaded_settings_to_ui AFTER applying the action in undo/redo methods
                 # self._apply_loaded_settings_to_ui() # Update UI elements for WM

            elif action_type == 'add_overlay':
                 overlay_uuid = action['overlay_data']['uuid']
                 if is_undo: # Undo Add = Remove
                      self._remove_overlay_internal(overlay_uuid)
                 else: # Redo Add = Add
                      overlay_data = action['overlay_data'].copy()
                      # Reload PIL image
                      overlay_data = self._reload_overlay_images([overlay_data])[0]
                      self._add_overlay_internal(overlay_data)
                 self._update_overlay_listbox()

            elif action_type == 'remove_overlay':
                 overlay_data = action['overlay_data'].copy()
                 if is_undo: # Undo Remove = Add back
                      # Reload PIL image
                      overlay_data = self._reload_overlay_images([overlay_data])[0]
                      self._add_overlay_internal(overlay_data)
                      # Restore original order if possible
                      if 'original_index' in action:
                           self._change_overlay_order_internal(overlay_data['uuid'], action['original_index'])
                 else: # Redo Remove = Remove again
                      self._remove_overlay_internal(overlay_data['uuid'])
                 self._update_overlay_listbox()

            elif action_type == 'reorder_overlay':
                 overlay_uuid = action['overlay_uuid']
                 target_index = action[target_prefix + 'index']
                 self._change_overlay_order_internal(overlay_uuid, target_index)
                 self._update_overlay_listbox()

            elif action_type == 'change_opacity':
                 area_type = action['area_type']
                 area_uuid = action['area_uuid']
                 target_value = action[target_prefix + 'value']
                 if area_type == 'wm':
                      settings['wm_img_info']['opacity'] = target_value
                      self.watermark_image_opacity.set(target_value) # Update UI too
                 elif area_type == 'overlay':
                      for o in settings['overlays']:
                           if o['uuid'] == area_uuid: o['opacity'] = target_value; break
                      # Update UI if this overlay is selected
                      if area_uuid == self.selected_overlay_uuid:
                           self.overlay_opacity_var.set(target_value)

            # --- Add Inverse Action to the Other Stack ---
            inverse_action = action.copy() # Shallow copy is ok for dict structure
            # Swap old/new values for simple value changes
            if 'old_value' in action and 'new_value' in action:
                 inverse_action['old_value'], inverse_action['new_value'] = action['new_value'], action['old_value']
            if 'old_values' in action and 'new_values' in action:
                 inverse_action['old_values'], inverse_action['new_values'] = action['new_values'], action['old_values']
            if 'old_rect' in action and 'new_rect' in action:
                 inverse_action['old_rect'], inverse_action['new_rect'] = action['new_rect'], action['old_rect']
            if 'old_angle' in action and 'new_angle' in action:
                 inverse_action['old_angle'], inverse_action['new_angle'] = action['new_angle'], action['old_angle']
            if 'old_index' in action and 'new_index' in action:
                 inverse_action['old_index'], inverse_action['new_index'] = action['new_index'], action['old_index']
            if 'old_rotation' in action and 'new_rotation' in action: # For image transform
                 inverse_action['old_rotation'], inverse_action['new_rotation'] = action['new_rotation'], action['old_rotation']
                 inverse_action['old_flip_h'], inverse_action['new_flip_h'] = action['new_flip_h'], action['old_flip_h']
                 inverse_action['old_flip_v'], inverse_action['new_flip_v'] = action['new_flip_v'], action['old_flip_v']
            # For Add/Remove/Clear, the action type itself implies the inverse

            if is_undo:
                 self.current_redo_stack.append(inverse_action)
            else: # is_redo
                 self.current_undo_stack.append(inverse_action)

            return True # Indicate success

        except Exception as e:
            print(f"Error applying action ({action_type}): {e}")
            import traceback
            traceback.print_exc()
            return False # Indicate failure


    # --- Actions tied to UI elements (calling undoable logic) ---

    def apply_preset_action(self, event=None):
        # Applying preset changes resize values, which isn't directly undoable currently.
        # Could make it undoable by storing old width/height/preset values.
        # For now, just apply directly.
        self.apply_preset()

    def apply_filter_action(self, event=None):
         if not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return

         old_filter = settings.get('filter', 'None') # Need to store filter per-image for undo
         new_filter = self.filter_var.get()

         if old_filter != new_filter:
             # --- Undo/Redo Logic ---
             action = {
                  'type': 'filter',
                  'old_value': old_filter,
                  'new_value': new_filter
             }
             self._add_undo_action(action)
             # ---
             settings['filter'] = new_filter # Store per-image setting
             self.update_preview_safe()


    def apply_transform_action(self, transform_type, value):
         """Applies rotation/flip to the base image - UNDOABLE ACTION."""
         if not self.original_image or not self.current_image_path: return
         settings = self.image_settings.get(self.current_image_path)
         if not settings: return

         old_rotation = settings.get('rotation', 0)
         old_flip_h = settings.get('flip_h', False)
         old_flip_v = settings.get('flip_v', False)

         new_rotation = old_rotation
         new_flip_h = old_flip_h
         new_flip_v = old_flip_v

         if transform_type == 'rotate':
              new_rotation = (old_rotation + value) % 360
         elif transform_type == 'flip':
              if value == "H": new_flip_h = not old_flip_h
              elif value == "V": new_flip_v = not old_flip_v

         if new_rotation != old_rotation or new_flip_h != old_flip_h or new_flip_v != old_flip_v:
             # --- Undo/Redo Logic ---
             action = {
                  'type': 'transform',
                  'area_type': 'image', # Special type for base image transform
                  'old_rotation': old_rotation, 'new_rotation': new_rotation,
                  'old_flip_h': old_flip_h, 'new_flip_h': new_flip_h,
                  'old_flip_v': old_flip_v, 'new_flip_v': new_flip_v,
             }
             self._add_undo_action(action)
             # ---

             settings['rotation'] = new_rotation
             settings['flip_h'] = new_flip_h
             settings['flip_v'] = new_flip_v

             # Apply the transforms to the base image
             self._apply_image_transforms_from_settings()

             # Transforms invalidate placement of manual items - warn user or reset?
             # Let's reset WM and Overlays for simplicity, edits remain but might look odd.
             if 'wm_img_info' in settings: self._reset_object_placement(settings['wm_img_info'])
             if 'overlays' in settings:
                  for ov in settings['overlays']: self._reset_object_placement(ov)
             self._update_overlay_listbox() # Reflect potential reset

             self.status_label.config(text=f"Image {transform_type} applied.")
             self.update_preview_safe() # Update the preview fully
             self._reset_interaction_states() # Deselect any areas


    def _apply_image_transforms_from_settings(self):
        """Applies rotation/flip stored in settings to self.original_image -> self.rotated_flipped_image"""
        if not self.original_image or not self.current_image_path:
            self.rotated_flipped_image = None
            return
        settings = self.image_settings.get(self.current_image_path)
        if not settings:
            self.rotated_flipped_image = self.original_image.copy() if self.original_image else None
            return

        img = self.original_image.copy()
        rotation = settings.get('rotation', 0)
        flip_h = settings.get('flip_h', False)
        flip_v = settings.get('flip_v', False)

        if flip_h: img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if flip_v: img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if rotation != 0: img = img.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)

        self.rotated_flipped_image = img


    def record_transform_action(self, area_type, area_uuid, old_rect, old_angle, new_rect, new_angle):
         """ Records move/resize/rotate of WM/Overlay/Edit - UNDOABLE ACTION """
         # Check if state actually changed
         if old_rect == new_rect and old_angle == new_angle:
             return # No change to record

         # --- Undo/Redo Logic ---
         action = {
              'type': 'transform',
              'area_type': area_type,
              'area_uuid': area_uuid,
              'old_rect': list(old_rect) if old_rect else None, # Store copies
              'old_angle': old_angle,
              'new_rect': list(new_rect) if new_rect else None,
              'new_angle': new_angle
         }
         self._add_undo_action(action)
         # ---
         print(f"Recorded transform for {area_type} {area_uuid}")


    # --- Conversion ---

    def confirm_conversion(self, single=False):
        """Shows confirmation dialog before starting conversion."""
        num_images = 1 if single else len(self.image_list)
        if num_images == 0:
            messagebox.showwarning("No Images", "Load image(s) first.")
            return
        if single and not self.current_image_path:
             messagebox.showwarning("No Image Selected", "Select an image tab first.")
             return

        target_image = os.path.basename(self.current_image_path) if single else f"{num_images} image(s)"
        output_dest = self.output_dir.get() if self.output_dir.get() else "Original folder(s)"
        filename_pattern = self.filename_var.get() if self.filename_var.get() else "<OriginalName>"
        if num_images > 1 and "<#>" not in filename_pattern:
             filename_pattern += "_<#>" # Add sequence if multiple images and no placeholder

        # Build summary message
        msg = f"Process {target_image}?\n\n"
        msg += f"Output Filename: {filename_pattern}.{self.format_var.get().lower()}\n"
        msg += f"Output Format: {self.format_var.get()}"
        if self.format_var.get() in ["JPEG", "WEBP"]: msg += f" (Quality: {self.quality_var.get()})"
        msg += "\n"

        # Resize Info
        preset = self.preset_var.get()
        w_str, h_str = self.resize_width_var.get(), self.resize_height_var.get()
        if preset != "Custom": msg += f"Resize Preset: {preset}\n"
        elif w_str or h_str: msg += f"Manual Resize: {w_str or 'auto'} x {h_str or 'auto'}\n"

        # Filter Info (Global for batch)
        if self.filter_var.get() != "None": msg += f"Filter: {self.filter_var.get()}\n"

        # --- Add specific info based on single/batch ---
        if single:
             settings = self.image_settings.get(self.current_image_path, {})
             adj = settings.get('adjustments', {})
             if any(abs(v-1.0) > 1e-6 for v in adj.values()): msg += f"Adjustments: Yes\n"
             if settings.get('blur_areas'): msg += f"Blur Areas: {len(settings['blur_areas'])}\n"
             if settings.get('blackout_areas'): msg += f"Blackout Areas: {len(settings['blackout_areas'])}\n"
             if self.use_text_watermark.get() and self.watermark_text.get(): msg += "Text Watermark: Yes\n"
             if self.use_image_watermark.get() and settings.get('wm_img_info', {}).get('path'): msg += "Image Watermark: Yes\n"
             if settings.get('overlays'): msg += f"Overlays: {len(settings['overlays'])}\n"
        else: # Batch
             # Mention which settings are global vs per-image
             msg += "\nGlobal Settings:\n"
             msg += f"- Filter: {self.filter_var.get()}\n" # Filter is global now
             if self.use_text_watermark.get() and self.watermark_text.get(): msg += "- Text Watermark: Yes\n"
             msg += "\nPer-Image Settings Applied:\n"
             msg += "- Rotation/Flip\n- Adjustments\n- Blur/Blackout Edits\n- Image Watermark (if enabled for image)\n- Overlays\n"

        msg += f"\nSave to: {output_dest}"

        if messagebox.askyesno("Confirm Conversion", msg):
            self.start_conversion_thread(single)
        else:
             self.status_label.config(text="Conversion cancelled.")


    def start_conversion_thread(self, single=False):
        """Starts the image conversion process in a separate thread."""
        images_to_process = []
        if single:
            if self.current_image_path:
                 images_to_process = [self.current_image_path]
            else: return # Should already be checked, but safe
        else:
             images_to_process = list(self.image_list) # Copy list

        if not images_to_process: return

        # --- Save settings for the currently viewed image BEFORE starting ---
        self._save_current_image_settings()
        # ---

        self.progress_bar['value'] = 0
        self.progress_bar['maximum'] = len(images_to_process)
        self.status_label.config(text="Starting conversion...")
        self.update_widget_states(processing=True) # Disable UI elements

        # --- Capture definitive GLOBAL batch settings BEFORE starting thread ---
        global_settings = {
            "preset_val": self.preset_var.get(),
            "resize_w_str": self.resize_width_var.get(),
            "resize_h_str": self.resize_height_var.get(),
            "filter_val": self.filter_var.get(),
            "output_format": self.format_var.get().lower(),
            "jpeg_quality": int(self.quality_var.get()) if self.format_var.get() in ["JPEG", "WEBP"] else 95,
            "base_output_dir": self.output_dir.get(),
            "filename_pattern": self.filename_var.get(),
            # Text WM (Always global)
            "use_text_wm": self.use_text_watermark.get(),
            "text_wm_text": self.watermark_text.get(),
            "text_wm_size": self.watermark_font_size.get(),
            "text_wm_color": self.watermark_color.get(),
            "text_wm_opacity": self.watermark_opacity.get(),
            "text_wm_pos": self.watermark_position.get(),
        }
        # ---

        # Deepcopy per-image settings to avoid thread conflicts if user switches tabs during conversion
        image_settings_copy = self._prepare_settings_for_save(self.image_settings) # Use helper to make serializable copy

        # Start the worker thread
        self.conversion_thread = threading.Thread(
             target=self.convert_images_worker,
             args=(list(images_to_process), global_settings, image_settings_copy), # Pass copies
             daemon=True
        )
        self.conversion_thread.start()


    def convert_images_worker(self, image_paths, global_settings, all_image_settings_copy):
        """Worker function that processes images in a background thread."""
        total = len(image_paths)
        success_count = 0
        fail_count = 0

        # --- Unpack global settings ---
        output_format = global_settings["output_format"]
        jpeg_quality = global_settings["jpeg_quality"]
        base_output_dir = global_settings["base_output_dir"]
        filename_pattern = global_settings["filename_pattern"]
        preset_val = global_settings["preset_val"]
        resize_w_str = global_settings["resize_w_str"]
        resize_h_str = global_settings["resize_h_str"]
        filter_val = global_settings["filter_val"]
        use_text_wm = global_settings["use_text_wm"]
        text_wm_text = global_settings["text_wm_text"]
        text_wm_size = global_settings["text_wm_size"]
        text_wm_color = global_settings["text_wm_color"]
        text_wm_opacity = global_settings["text_wm_opacity"]
        text_wm_pos = global_settings["text_wm_pos"]
        # ---

        # --- Pre-load overlay/WM images used across all images (if any) ---
        # This requires identifying common paths, potentially complex.
        # Current approach: Load images individually within the loop for simplicity & correctness.


        for idx, image_path in enumerate(image_paths, 1):
            # --- Get Per-Image Settings from the passed *copy* ---
            current_image_settings = all_image_settings_copy.get(image_path, self._get_default_image_settings())

            # Extract per-image data
            rotation = current_image_settings.get('rotation', 0)
            flip_h = current_image_settings.get('flip_h', False)
            flip_v = current_image_settings.get('flip_v', False)
            adj_settings = current_image_settings.get('adjustments', {'brightness': 1.0, 'contrast': 1.0, 'saturation': 1.0})
            blur_areas_list = current_image_settings.get('blur_areas', [])
            blackout_areas_list = current_image_settings.get('blackout_areas', [])
            # wm_info = current_image_settings.get('wm_img_info') # WM is now global
            overlays_list = current_image_settings.get('overlays', []) # Contains path, rect, angle, opacity

            # Update status (using root.after for thread safety)
            self.root.after(0, lambda i=idx, p=image_path: self.status_label.config(
                text=f"Processing {i}/{total}: {os.path.basename(p)}..."
            ))

            try:
                # --- 1. Load Base Image & Apply EXIF ---
                img_base = Image.open(image_path)
                img_oriented = self._apply_exif_orientation(img_base)
                # Store original size AFTER orientation for scaling calculations later
                original_w_oriented, original_h_oriented = img_oriented.size

                # --- 2. Apply Image Transforms (Rot/Flip) ---
                img = img_oriented.copy() # Start from oriented version
                if flip_h: img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                if flip_v: img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                if rotation != 0: img = img.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)

                # --- 3. Apply Filter (Global) ---
                img = self.apply_filter(img, filter_val)

                # --- 4. Apply Resize (Global/Preset) ---
                img = self.apply_resize(img, preset_val, resize_w_str, resize_h_str)

                # --- 5. Apply Adjustments (Per Image) ---
                img = self.apply_adjustments(img, adj_settings)

                # --- Ensure RGBA if needed for next steps ---
                needs_rgba = (blur_areas_list or blackout_areas_list or use_text_wm or
                              (self.use_image_watermark.get() and self.wm_img_info.get('path')) or overlays_list)
                if needs_rgba and img.mode != 'RGBA':
                     img = img.convert('RGBA')

                # --- 6. Apply Manual Edits (Per Image) ---
                # Pass ORIENTED original dimensions for coordinate scaling
                img = self.apply_manual_edits(img, blur_areas_list, blackout_areas_list) # Now uses self.original_image which might be wrong? NO, pass dims
                # **REVISED CALL** Pass the correct original dimensions for scaling
                temp_original_image = Image.new('RGB', (original_w_oriented, original_h_oriented)) # Dummy image with correct size
                img = self._apply_manual_edits_conversion_safe(img, blur_areas_list, blackout_areas_list, temp_original_image)
                del temp_original_image


                # --- 7. Apply Text Watermark (Global) ---
                if use_text_wm and text_wm_text:
                     img = self.apply_text_watermark(img) # Uses global settings directly

                # --- 8. Apply Main Image Watermark (Global) ---
                # Use global settings: self.use_image_watermark and self.wm_img_info
                if self.use_image_watermark.get() and self.wm_img_info.get('path'):
                     try:
                          # Reload the global WM image info (in case file changed, though unlikely in worker)
                          # Pass a copy to avoid modifying the main dict if reload fails partially
                          reloaded_wm_info = self._reload_wm_image(self.wm_img_info.copy())
                          if reloaded_wm_info and reloaded_wm_info.get('pil_image'):
                               # Pass oriented original dimensions
                               temp_original_image = Image.new('RGB', (original_w_oriented, original_h_oriented))
                               img = self._apply_single_image_overlay_conversion_safe(img, reloaded_wm_info, temp_original_image)
                               del temp_original_image
                     except Exception as wm_load_err:
                          print(f"Error loading WM image '{wm_info.get('path')}' during conversion: {wm_load_err}")

                # --- 9. Apply Overlays (Per Image) ---
                if overlays_list:
                    temp_original_image = Image.new('RGB', (original_w_oriented, original_h_oriented)) # Create dummy once
                    processed_overlays = []
                    for ov in overlays_list:
                         try:
                              reloaded_ov = self._reload_overlay_images([ov.copy()])[0]
                              if reloaded_ov['pil_image']:
                                   processed_overlays.append(reloaded_ov)
                         except Exception as ov_load_err:
                              print(f"Error loading overlay '{ov.get('path')}' during conversion: {ov_load_err}")
                    # Apply overlays using the correctly sized dummy original
                    img = self._apply_overlays_conversion_safe(img, processed_overlays, temp_original_image)
                    del temp_original_image


                # --- 10. Determine Output Path ---
                output_dir_for_file = base_output_dir if base_output_dir else os.path.dirname(image_path)
                os.makedirs(output_dir_for_file, exist_ok=True)
                original_basename = os.path.splitext(os.path.basename(image_path))[0]
                out_name = self._generate_output_filename(filename_pattern, original_basename, idx, total)
                safe_format = output_format if output_format != 'jpeg' else 'jpg'
                output_path = os.path.join(output_dir_for_file, f"{out_name}.{safe_format}")


                # --- 11. Save Image ---
                save_params = {}
                final_img_to_save = img
                if output_format == "jpeg":
                    if final_img_to_save.mode in ['RGBA', 'LA', 'P']:
                        background = Image.new("RGB", final_img_to_save.size, (255, 255, 255))
                        try: background.paste(final_img_to_save, mask=final_img_to_save.split()[3])
                        except IndexError: background.paste(final_img_to_save)
                        final_img_to_save = background
                    elif final_img_to_save.mode != 'RGB':
                         final_img_to_save = final_img_to_save.convert('RGB')
                    save_params = {"quality": jpeg_quality, "optimize": True, "progressive": True}
                elif output_format == "png": save_params = {"optimize": True}
                elif output_format == "webp": save_params = {"quality": jpeg_quality, "lossless": False} # Add lossless option?

                final_img_to_save.save(output_path, output_format.upper(), **save_params)
                success_count += 1

            except Exception as e:
                fail_count += 1
                error_msg = f"Failed to process {os.path.basename(image_path)}:\n{e}"
                print(error_msg)
                import traceback
                traceback.print_exc()
                # Show error message in main thread
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Conversion Error", msg))

            # Update progress bar
            self.root.after(0, lambda i=idx: self.progress_bar.config(value=i))
            time.sleep(0.01) # Yield


        # --- Conversion Finished ---
        final_status = f"Conversion complete. Success: {success_count}, Failed: {fail_count}."
        print(final_status)
        self.root.after(0, lambda: self.status_label.config(text=final_status))
        self.root.after(0, lambda: self.update_widget_states(processing=False)) # Re-enable UI

    # --- Conversion Helper Methods (Thread-Safe Alternatives) ---

    def _apply_manual_edits_conversion_safe(self, img, blur_areas_list, blackout_areas_list, original_img_ref):
        """Thread-safe version of apply_manual_edits using passed original dimensions."""
        if not blur_areas_list and not blackout_areas_list: return img
        if not original_img_ref:
             print("Warning: Cannot apply manual edits during conversion - original image reference missing.")
             return img

        editable_img = img if img.mode == 'RGBA' else img.convert('RGBA')
        draw = ImageDraw.Draw(editable_img)
        current_w, current_h = editable_img.size
        original_w, original_h = original_img_ref.size # Get dims from reference

        scale_x = current_w / original_w if original_w > 0 else 1
        scale_y = current_h / original_h if original_h > 0 else 1

        # Apply Blur Areas
        for area in blur_areas_list:
             shape, coords_orig, strength = area['shape'], area['coords'], area['strength']
             ox0, oy0, ox1, oy1 = coords_orig
             scaled_coords = (ox0 * scale_x, oy0 * scale_y, ox1 * scale_x, oy1 * scale_y)
             crop_region = (max(0, int(scaled_coords[0])), max(0, int(scaled_coords[1])),
                           min(current_w, int(scaled_coords[2])), min(current_h, int(scaled_coords[3])))

             if crop_region[2] > crop_region[0] and crop_region[3] > crop_region[1]:
                  try:
                      cropped = editable_img.crop(crop_region)
                      effective_strength = min(strength, max(cropped.width, cropped.height)/4) if cropped.width > 0 and cropped.height > 0 else strength
                      blurred_cropped = cropped.filter(ImageFilter.GaussianBlur(radius=effective_strength))
                      if shape == 'rectangle': editable_img.paste(blurred_cropped, crop_region)
                      elif shape == 'circle':
                          mask = Image.new('L', cropped.size, 0); mask_draw = ImageDraw.Draw(mask)
                          mask_draw.ellipse((0, 0, cropped.width-1, cropped.height-1), fill=255)
                          editable_img.paste(blurred_cropped, crop_region, mask); del mask_draw
                  except Exception as e: print(f"Thread Blur application error: {e}")

        # Apply Blackout Areas
        for area in blackout_areas_list:
             shape, coords_orig = area['shape'], area['coords']
             ox0, oy0, ox1, oy1 = coords_orig
             scaled_coords = [max(0, int(ox0 * scale_x)), max(0, int(oy0 * scale_y)),
                             min(current_w, int(ox1 * scale_x)), min(current_h, int(oy1 * scale_y))]
             if scaled_coords[2] > scaled_coords[0] and scaled_coords[3] > scaled_coords[1]:
                  try:
                      if shape == 'rectangle': draw.rectangle(scaled_coords, fill="black")
                      elif shape == 'circle': draw.ellipse(scaled_coords, fill="black")
                  except Exception as e: print(f"Thread Blackout application error: {e}")

        del draw
        return editable_img

    def _apply_single_image_overlay_conversion_safe(self, img, overlay_info, original_img_ref):
        """Thread-safe version of apply_single_image_overlay."""
        if not overlay_info or not overlay_info.get('pil_image') or not overlay_info.get('rect'): return img
        if not original_img_ref:
             print("Warning: Cannot apply overlay during conversion - original image reference missing.")
             return img

        if img.mode != 'RGBA': img = img.convert('RGBA')

        try:
            wm_img_original = overlay_info['pil_image'].copy()
            opacity = max(0, min(255, overlay_info.get('opacity', 128)))
            rect_orig = overlay_info['rect']
            angle = overlay_info.get('angle', 0.0)

            if opacity < 255:
                 try:
                     alpha = wm_img_original.split()[3]
                     alpha = alpha.point(lambda p: int(p * (opacity / 255.0)))
                     wm_img_original.putalpha(alpha)
                 except IndexError: pass

            current_w, current_h = img.size
            original_w, original_h = original_img_ref.size # Use passed reference dims

            scale_x = current_w / original_w if original_w > 0 else 1
            scale_y = current_h / original_h if original_h > 0 else 1

            ox0, oy0, ox1, oy1 = rect_orig
            scaled_rect = (ox0 * scale_x, oy0 * scale_y, ox1 * scale_x, oy1 * scale_y)
            target_w = int(scaled_rect[2] - scaled_rect[0])
            target_h = int(scaled_rect[3] - scaled_rect[1])

            if target_w <= 0 or target_h <= 0: return img

            wm_resized = wm_img_original.resize((target_w, target_h), Image.Resampling.LANCZOS)
            wm_rotated = wm_resized.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
            rot_w, rot_h = wm_rotated.size

            center_x = (scaled_rect[0] + scaled_rect[2]) / 2
            center_y = (scaled_rect[1] + scaled_rect[3]) / 2
            paste_x = int(center_x - rot_w / 2)
            paste_y = int(center_y - rot_h / 2)

            wm_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            wm_layer.paste(wm_rotated, (paste_x, paste_y), wm_rotated)
            return Image.alpha_composite(img, wm_layer)

        except Exception as e:
            path_hint = os.path.basename(overlay_info.get('path','Unknown')) if overlay_info else 'Unknown'
            print(f"Error applying image overlay/WM ({path_hint}) during conversion: {e}")
            return img

    def _apply_overlays_conversion_safe(self, img, overlays_list, original_img_ref):
        """Thread-safe version of apply_overlays."""
        if not overlays_list: return img
        base_img = img if img.mode == 'RGBA' else img.convert('RGBA')
        for overlay_info in overlays_list:
             base_img = self._apply_single_image_overlay_conversion_safe(base_img, overlay_info, original_img_ref)
        return base_img


    def _generate_output_filename(self, pattern, original_name, index, total):
         """ Generates the output filename based on pattern and index. """
         if not pattern: pattern = "<OriginalName>" # Default if empty

         # Basic sanitization
         sanitized_pattern = pattern.replace("<ext>", "").replace("<EXT>", "")
         # Replace placeholders
         if "<OriginalName>" in sanitized_pattern:
             out_name = sanitized_pattern.replace("<OriginalName>", original_name)
         else:
             out_name = sanitized_pattern # Use pattern directly if no original name placeholder

         if "<#>" in out_name:
             # Format index with leading zeros based on total count
             num_digits = len(str(total))
             out_name = out_name.replace("<#>", f"{index:0{num_digits}d}")
         elif total > 1 and pattern == "<OriginalName>":
              # Multiple files, using original names - might overwrite! Add index for safety.
              out_name = f"{original_name}_{index}"
         elif total > 1 and "<#>" not in pattern and pattern != "<OriginalName>":
               # Multiple files, custom static pattern - add index to prevent overwrite
               out_name = f"{out_name}_{index}"

         # Final check for empty name
         if not out_name.strip():
              out_name = f"image_{index}"

         # Further sanitize illegal filename characters (basic example)
         illegal_chars = r'<>:"/\|?*'
         for char in illegal_chars:
              out_name = out_name.replace(char, '_')

         return out_name


    # --- Presets and Settings Persistence ---

    def apply_preset(self):
        """Applies selected size preset (not undoable directly)."""
        preset = self.preset_var.get()
        dimensions = {
            "YouTube Thumbnail (1280x720)": ("1280", "720"),
            "Facebook Post (1200x630)": ("1200", "630"),
            "Instagram Post (1080x1080)": ("1080", "1080"),
            "Twitter Post (1024x512)": ("1024", "512")
        }
        width, height = dimensions.get(preset, ("", ""))
        self.resize_width_var.set(width)
        self.resize_height_var.set(height)
        self.update_widget_states()
        self.update_preview_safe() # Preview reflects resize immediately


    def reset_all(self):
        """Resets global settings and clears all image data and states."""
        if messagebox.askyesno("Confirm Reset", "Reset all global settings and clear all loaded images and their edits?"):
            # Save state of current image before clearing (though it will be lost)
            self._save_current_image_settings()
            # Clear image data and related states
            self.clear_state(clear_image_list=True)

            # Reset global settings variables to defaults
            self.output_dir.set("")
            self.preset_var.set("Custom")
            self.format_var.set("PNG")
            self.quality_var.set("95")
            self.resize_width_var.set("")
            self.resize_height_var.set("")
            self.filter_var.set("None")
            self.theme_var.set("arc") # Reset theme choice too

            # Reset Text Watermark settings
            self.watermark_text.set("SAMPLE")
            self.watermark_font_size.set(40)
            self.watermark_color.set("#FFFFFF")
            self.watermark_opacity.set(128)
            self.watermark_position.set("Diagonal Fit")
            self.use_text_watermark.set(False)

            # Clear visuals and info labels
            self.preview_canvas.delete("all")
            self.image_info_label.config(text="Load an image to see preview and info.")
            self.dnd_label.config(text="Drop images or folders here" if _tkdnd_available else "Drag & Drop (Disabled)")
            self.status_label.config(text="All settings and images reset.")
            self.progress_bar['value'] = 0
            self.zoom_factor = 1.0
            self.pan_offset = [0, 0]
            self._update_zoom_label()

            # Apply default theme visually
            self.change_theme_action()

            # Update widget enable/disable states
            self.update_widget_states()
            self.update_undo_redo_buttons()


    def clear_state(self, clear_image_list=False):
        """Clears image-specific data and potentially the image list."""
        # Save current image state just before clearing it
        if self.current_image_path:
             self._save_current_image_settings()

        if clear_image_list:
            self.image_list = []
            self.image_settings = {} # Clear ALL per-image settings
            try: # Clear notebook tabs
                if hasattr(self, 'image_notebook') and self.image_notebook.winfo_exists():
                     for tab_id in self.image_notebook.tabs(): self.image_notebook.forget(tab_id)
                     self.image_notebook.grid_remove()
            except Exception as e:
                 print(f"Error clearing notebook: {e}")


        # Clear current image display and state
        self.original_image = None
        self.rotated_flipped_image = None
        self.processed_image = None
        self.preview_image_tk = None
        self.current_image_path = None
        self.filename_var.set("")
        self.processed_base_size = None

        # Reset interaction states
        self._reset_interaction_states()

        # Reset UI elements tied to per-image state
        self.brightness_var.set(1.0)
        self.contrast_var.set(1.0)
        self.saturation_var.set(1.0)
        self.watermark_image_path.set("")
        self.watermark_image_opacity.set(128)
        self.use_image_watermark.set(False) # Checkbox depends on loaded path now
        self._update_overlay_listbox()
        self.current_undo_stack.clear()
        self.current_redo_stack.clear()
        self.zoom_factor = 1.0
        self.pan_offset = [0,0]
        self._update_zoom_label()

        # Clear visuals
        self.preview_canvas.delete("all")
        self.image_info_label.config(text="Load an image to see preview and info.")
        self.progress_bar['value'] = 0

        # Don't reset global settings like output dir, format, quality, filter, theme etc. here
        # Update widgets and undo/redo state
        self.update_widget_states()
        self.update_undo_redo_buttons()


    def _save_current_image_settings(self):
        """Saves the current UI/state settings to self.image_settings for the current image path."""
        if not self.current_image_path or self.current_image_path not in self.image_settings:
            return # No current image or settings entry doesn't exist

        settings = self.image_settings[self.current_image_path]

        # Overwrite relevant keys with current state
        # Image transform state (rotation/flip) is already in settings via transform actions
        # Filter state is already in settings via filter actions

        settings['adjustments'] = {
            'brightness': self.brightness_var.get(),
            'contrast': self.contrast_var.get(),
            'saturation': self.saturation_var.get()
        }

        # WM info (opacity might have changed via UI scale without a direct action)
        if 'wm_img_info' in settings and settings['wm_img_info']:
            settings['wm_img_info']['opacity'] = self.watermark_image_opacity.get()

        # Overlays list (opacity might have changed via UI scale)
        if 'overlays' in settings and self.selected_area_type == 'overlay' and self.selected_area_uuid:
             overlay = next((o for o in settings['overlays'] if o['uuid'] == self.selected_area_uuid), None)
             if overlay:
                  overlay['opacity'] = self.overlay_opacity_var.get()

        # Blur/Blackout areas (updated by actions)
        # Undo/Redo stacks (updated by actions)

        # Note: No need to call _prepare_settings_for_save here, that's for file saving.
        # We want to keep the PIL images in the live state dict.

        print(f"State updated in memory for {os.path.basename(self.current_image_path)}")


    def _prepare_settings_for_save(self, settings_dict_or_full_dict):
        """
        Creates a deep copy of settings, removing non-serializable PIL images.
        Can accept either a single image's settings dict or the full self.image_settings dict.
        """
        import copy
        # Determine if we are saving one image's settings or the whole dict
        if isinstance(settings_dict_or_full_dict.get('undo_stack', None), deque): # Heuristic: Check for deque in top level
             is_single_image = True
             settings_to_process = {'temp_key': settings_dict_or_full_dict} # Wrap it for uniform processing
        else:
             is_single_image = False
             settings_to_process = settings_dict_or_full_dict

        save_dict = copy.deepcopy(settings_to_process) # Deep copy

        for img_path, settings in save_dict.items():
            if not isinstance(settings, dict): continue # Skip if format is unexpected

            # Remove PIL image from main WM info
            if 'wm_img_info' in settings and isinstance(settings['wm_img_info'], dict):
                 settings['wm_img_info'].pop('pil_image', None) # Use pop with default None
            # Remove PIL images from overlays
            if 'overlays' in settings and isinstance(settings['overlays'], list):
                 for overlay in settings['overlays']:
                      if isinstance(overlay, dict):
                           overlay.pop('pil_image', None)
            # Convert deques to lists for JSON
            if 'undo_stack' in settings:
                 settings['undo_stack'] = list(settings['undo_stack'])
            if 'redo_stack' in settings:
                 settings['redo_stack'] = list(settings['redo_stack'])

        # If we wrapped a single image's settings, unwrap it now
        if is_single_image:
             return save_dict.get('temp_key', {})
        else:
             return save_dict


    def save_presets(self):
        """Saves current GLOBAL settings to a JSON file. Per-image settings NOT saved here."""
        global_settings = {
            "version": 2.0, # Add version number
            "format": self.format_var.get(),
            "quality": self.quality_var.get(),
            "preset": self.preset_var.get(), # Save preset choice
            "resize_w": self.resize_width_var.get(), # Save manual resize too
            "resize_h": self.resize_height_var.get(),
            "filter": self.filter_var.get(), # Save global default filter
            "output_dir": self.output_dir.get(),
            "theme": self.theme_var.get(),
            # Text WM settings
            "watermark_text": self.watermark_text.get(),
            "watermark_font_size": self.watermark_font_size.get(),
            "watermark_color": self.watermark_color.get(),
            "watermark_opacity": self.watermark_opacity.get(),
            "watermark_position": self.watermark_position.get(),
            "use_text_watermark": self.use_text_watermark.get(),
            # Global Image WM settings
            "use_image_watermark": self.use_image_watermark.get(),
            "wm_img_path": self.wm_img_info.get('path'),
            "wm_img_rect": self.wm_img_info.get('rect'),
            "wm_img_angle": self.wm_img_info.get('angle', 0.0),
            "wm_img_opacity": self.wm_img_info.get('opacity', 128),
        }
        try:
            with open("image_master_settings_v2.json", "w") as f:
                json.dump(global_settings, f, indent=4)
            self.status_label.config(text="Global settings saved successfully.")
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to save global settings:\n{e}")


    def load_presets(self):
        """Loads GLOBAL settings from the JSON file if it exists."""
        settings_file = "image_master_settings_v2.json" # Look for new version
        if not os.path.exists(settings_file):
             self.status_label.config(text="No saved settings file found.")
             return

        try:
            with open(settings_file, "r") as f:
                settings = json.load(f)

            # Load settings, providing defaults if keys are missing
            self.format_var.set(settings.get("format", "PNG"))
            self.quality_var.set(settings.get("quality", "95"))
            self.preset_var.set(settings.get("preset", "Custom"))
            self.resize_width_var.set(settings.get("resize_w", ""))
            self.resize_height_var.set(settings.get("resize_h", ""))
            self.filter_var.set(settings.get("filter", "None")) # Global filter default
            self.output_dir.set(settings.get("output_dir", ""))

            saved_theme = settings.get("theme", "arc")
            if saved_theme in self.available_themes: self.theme_var.set(saved_theme)
            else: self.theme_var.set("arc") # Default if saved theme invalid

            # Text WM settings
            self.watermark_text.set(settings.get("watermark_text", "SAMPLE"))
            self.watermark_font_size.set(settings.get("watermark_font_size", 40))
            self.watermark_color.set(settings.get("watermark_color", "#FFFFFF"))
            self.watermark_opacity.set(settings.get("watermark_opacity", 128))
            self.watermark_position.set(settings.get("watermark_position", "Diagonal Fit"))
            self.use_text_watermark.set(settings.get("use_text_watermark", False))

            # Global Image WM settings
            self.use_image_watermark.set(settings.get("use_image_watermark", False))
            loaded_wm_info = {
                'path': settings.get("wm_img_path"),
                'rect': settings.get("wm_img_rect"),
                'angle': settings.get("wm_img_angle", 0.0),
                'opacity': settings.get("wm_img_opacity", 128),
                'pil_image': None # Needs reloading
            }
            # Reload PIL image if path exists
            self.wm_img_info = self._reload_wm_image(loaded_wm_info)
            # Update UI elements linked to global state
            self.watermark_image_path.set(self.wm_img_info.get('path', "") or "")
            self.watermark_image_opacity.set(self.wm_img_info.get('opacity', 128))


            # Apply loaded preset if not custom (this updates resize entries)
            if self.preset_var.get() != "Custom":
                 self.apply_preset()

            # Theme is applied during init_style/change_theme
            self.status_label.config(text="Global settings loaded successfully.")

        except json.JSONDecodeError:
             messagebox.showwarning("Load Error", f"Failed to decode settings file '{settings_file}'. It might be corrupted.")
             self.status_label.config(text="Error loading settings: Invalid format.")
        except Exception as e:
            messagebox.showwarning("Load Error", f"Failed to load settings:\n{e}")
            self.status_label.config(text="Error loading settings.")
        finally:
             # Update states AFTER attempting load, even if failed
             self.update_widget_states()


# --- Utility Functions ---
    def _rotate_point(self, cx, cy, px, py, angle_degrees):
        """ Rotates point (px, py) around center (cx, cy) by angle_degrees. """
        angle_rad = math.radians(angle_degrees)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        px_rel = px - cx
        py_rel = py - cy
        new_x = cx + px_rel * cos_a - py_rel * sin_a
        new_y = cy + px_rel * sin_a + py_rel * cos_a
        return new_x, new_y


# --- Application Entry Point ---
if __name__ == "__main__":
    # Set high DPI awareness on Windows if possible
    try:
        from ctypes import windll
        # Values: 0=unaware, 1=system aware, 2=per-monitor aware
        # Try 2 first, fall back to 1
        try:
            windll.shcore.SetProcessDpiAwareness(2)
            print("Set DPI Awareness to Per-Monitor Aware (2)")
        except AttributeError: # shcore not available on older Windows
            try:
                windll.user32.SetProcessDPIAware()
                print("Set DPI Awareness via user32.SetProcessDPIAware()")
            except AttributeError:
                print("Could not set DPI awareness.")
    except ImportError:
         print("Could not import ctypes (not on Windows?). DPI awareness not set.")
    except Exception as dpi_e:
         print(f"Error setting DPI awareness: {dpi_e}")


    try:
        # Initialize main window using TkinterDnD wrapper if available
        root = TkinterDnD.Tk()
        root.withdraw() # Hide the default empty window initially

        # Create and run the application
        app = ImageMasterProApp(root)
        root.deiconify() # Show window after UI is built
        root.mainloop()

    except Exception as e:
         # Catch fatal startup errors
         import traceback
         traceback.print_exc()
         try: # Try showing a messagebox if Tk is minimally functional
              error_root = tk.Tk()
              error_root.withdraw()
              messagebox.showerror("Fatal Startup Error", f"ImageMaster Pro failed to start:\n\n{e}\n\nSee console for details.")
              error_root.destroy()
         except:
              print(f"FATAL: Application failed to start: {e}")
         sys.exit(1)