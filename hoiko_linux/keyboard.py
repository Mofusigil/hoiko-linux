"""Keyboard backend for Proton's XWayland window; only the game receives presses."""
import ctypes as c
import json

DEFAULT_KEYS = {"left": "Left", "right": "Right", "up": "Up", "down": "Down",
                "a": "z", "b": "x", "lb": "c", "start": "Return", "back": "Escape"}


class ClientData(c.Union):
    _fields_ = [("bytes", c.c_char * 20), ("shorts", c.c_short * 10), ("longs", c.c_long * 5)]


class ClientMessage(c.Structure):
    _fields_ = [("type", c.c_int), ("serial", c.c_ulong), ("send_event", c.c_int),
                ("display", c.c_void_p), ("window", c.c_ulong), ("message_type", c.c_ulong),
                ("format", c.c_int), ("data", ClientData)]


class XEvent(c.Union):
    _fields_ = [("client", ClientMessage), ("padding", c.c_long * 24)]


class Keyboard:
    def __init__(self, pid, mapping=None):
        self.pid, self.pressed, self.closed = pid, None, False
        self.x11, self.xtst = c.CDLL("libX11.so.6"), c.CDLL("libXtst.so.6")
        self.x11.XOpenDisplay.argtypes = [c.c_char_p]
        self.x11.XOpenDisplay.restype = c.c_void_p
        self.display = self.x11.XOpenDisplay(None)
        if not self.display: raise RuntimeError("No X11/XWayland display available")
        self.x11.XDefaultRootWindow.argtypes = [c.c_void_p]
        self.x11.XDefaultRootWindow.restype = c.c_ulong
        self.x11.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
        self.x11.XInternAtom.restype = c.c_ulong
        self.x11.XGetWindowProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong,
            c.c_long, c.c_long, c.c_int, c.c_ulong, c.POINTER(c.c_ulong),
            c.POINTER(c.c_int), c.POINTER(c.c_ulong), c.POINTER(c.c_ulong), c.POINTER(c.c_void_p)]
        self.x11.XFree.argtypes = [c.c_void_p]
        self.x11.XFlush.argtypes = [c.c_void_p]
        self.x11.XCloseDisplay.argtypes = [c.c_void_p]
        self.x11.XStringToKeysym.argtypes = [c.c_char_p]
        self.x11.XStringToKeysym.restype = c.c_ulong
        self.x11.XKeysymToKeycode.argtypes = [c.c_void_p, c.c_ulong]
        self.x11.XKeysymToKeycode.restype = c.c_uint
        self.xtst.XTestFakeKeyEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, c.c_ulong]
        self.mapping = DEFAULT_KEYS.copy()
        if mapping: self.mapping.update(mapping)
        self.keycodes = {name: self.x11.XKeysymToKeycode(self.display,
                        self.x11.XStringToKeysym(key.encode())) for name, key in self.mapping.items()}
        if not all(self.keycodes.values()):
            self.close(); raise RuntimeError("Unknown X11 key name in keyboard mapping")

    def property(self, window, name):
        atom = self.x11.XInternAtom(self.display, name.encode(), 1)
        typ, fmt, count, remaining, pointer = c.c_ulong(), c.c_int(), c.c_ulong(), c.c_ulong(), c.c_void_p()
        if self.x11.XGetWindowProperty(self.display, window, atom, 0, 1, 0, 0,
                c.byref(typ), c.byref(fmt), c.byref(count), c.byref(remaining), c.byref(pointer)):
            return None
        try:
            return c.cast(pointer, c.POINTER(c.c_ulong))[0] if pointer.value and count.value and fmt.value == 32 else None
        finally:
            if pointer.value: self.x11.XFree(pointer)

    def focused(self):
        root = self.x11.XDefaultRootWindow(self.display)
        active = self.property(root, "_NET_ACTIVE_WINDOW")
        return bool(active and self.property(active, "_NET_WM_PID") == self.pid)

    def activate(self):
        """Request activation of the title-matched window belonging to this game PID."""
        root = self.x11.XDefaultRootWindow(self.display)
        self.x11.XQueryTree.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong),
            c.POINTER(c.c_ulong), c.POINTER(c.POINTER(c.c_ulong)), c.POINTER(c.c_uint)]
        children, count = c.POINTER(c.c_ulong)(), c.c_uint()
        parent, result_root = c.c_ulong(), c.c_ulong()
        self.x11.XQueryTree(self.display, root, c.byref(result_root), c.byref(parent),
                           c.byref(children), c.byref(count))
        game_window = None
        try:
            for i in range(count.value):
                window = children[i]
                if self.property(window, "_NET_WM_PID") != self.pid: continue
                name_atom = self.x11.XInternAtom(self.display, b"_NET_WM_NAME", 1)
                typ, fmt, n, extra, pointer = c.c_ulong(), c.c_int(), c.c_ulong(), c.c_ulong(), c.c_void_p()
                self.x11.XGetWindowProperty(self.display, window, name_atom, 0, 256, 0, 0,
                    c.byref(typ), c.byref(fmt), c.byref(n), c.byref(extra), c.byref(pointer))
                try:
                    title = c.string_at(pointer, n.value).decode(errors="replace") if pointer.value and fmt.value == 8 else ""
                    if "puyo" in title.lower() or "tetris" in title.lower(): game_window = window
                finally:
                    if pointer.value: self.x11.XFree(pointer)
        finally:
            if children: self.x11.XFree(children)
        if game_window is None: raise RuntimeError("Cannot find the PPT game window")
        event = XEvent()
        event.client.type, event.client.send_event = 33, 1
        event.client.display, event.client.window = self.display, game_window
        event.client.message_type = self.x11.XInternAtom(self.display, b"_NET_ACTIVE_WINDOW", 0)
        event.client.format = 32
        event.client.data.longs[0] = 2
        self.x11.XSendEvent.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_long, c.POINTER(XEvent)]
        self.x11.XSendEvent(self.display, root, 0, (1 << 19) | (1 << 20), c.byref(event))
        self.x11.XFlush(self.display)

    def press(self, button):
        self.release()
        if not self.focused():
            raise RuntimeError("Focus the PPT game window to use the keyboard backend")
        code = self.keycodes[button]
        if not self.xtst.XTestFakeKeyEvent(self.display, code, 1, 0):
            raise RuntimeError("XTest key press failed")
        self.x11.XFlush(self.display)
        self.pressed = code

    def release(self):
        if not self.closed and self.pressed:
            self.xtst.XTestFakeKeyEvent(self.display, self.pressed, 0, 0)
            self.x11.XFlush(self.display); self.pressed = None

    def close(self):
        if self.closed: return
        self.release(); self.x11.XCloseDisplay(self.display); self.closed = True
