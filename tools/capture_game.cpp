// Diagnostic capture of the title-matched XWayland game window only.
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    const unsigned long pid = std::strtoul(argv[1], nullptr, 10);
    Display* display = XOpenDisplay(nullptr);
    if (!display) return 3;
    Window root = DefaultRootWindow(display), returned_root, parent, *children = nullptr;
    unsigned count = 0;
    XQueryTree(display, root, &returned_root, &parent, &children, &count);
    Window target = 0;
    for (unsigned i = 0; i < count; ++i) {
        Atom type; int format; unsigned long n, extra; unsigned char* data = nullptr;
        XGetWindowProperty(display, children[i], XInternAtom(display, "_NET_WM_PID", True),
            0, 1, False, AnyPropertyType, &type, &format, &n, &extra, &data);
        bool own = data && format == 32 && n && *reinterpret_cast<unsigned long*>(data) == pid;
        if (data) XFree(data);
        if (!own) continue;
        char* title = nullptr;
        XFetchName(display, children[i], &title);
        std::string name = title ? title : "";
        if (title) XFree(title);
        std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
        if (name.find("puyo") != std::string::npos || name.find("tetris") != std::string::npos)
            target = children[i];
    }
    if (children) XFree(children);
    if (!target) { XCloseDisplay(display); return 4; }
    XWindowAttributes attributes{};
    XGetWindowAttributes(display, target, &attributes);
    XImage* frame = XGetImage(display, target, 0, 0, attributes.width, attributes.height, AllPlanes, ZPixmap);
    if (!frame) { XCloseDisplay(display); return 5; }
    auto component = [](unsigned long pixel, unsigned long mask) {
        if (!mask) return 0ul;
        unsigned shift = 0;
        while (!(mask & 1)) { mask >>= 1; ++shift; }
        return ((pixel >> shift) & mask) * 255 / mask;
    };
    std::ofstream output(argv[2], std::ios::binary);
    output << "P6\n" << frame->width << ' ' << frame->height << "\n255\n";
    for (int y = 0; y < frame->height; ++y)
        for (int x = 0; x < frame->width; ++x) {
            auto pixel = XGetPixel(frame, x, y);
            char rgb[3] = {static_cast<char>(component(pixel, frame->red_mask)),
                           static_cast<char>(component(pixel, frame->green_mask)),
                           static_cast<char>(component(pixel, frame->blue_mask))};
            output.write(rgb, 3);
        }
    std::cout << frame->width << "x" << frame->height << '\n';
    XDestroyImage(frame); XCloseDisplay(display);
    return output ? 0 : 6;
}
