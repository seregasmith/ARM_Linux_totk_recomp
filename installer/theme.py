"""Night mode of the installer window (installer/gui.py): the box next to the language switch.

Light mode is the ttk theme Tk starts with, left as it is. Night mode is a second theme, made from the first when the
box is first ticked (Night.build): every style setting is copied, and each colour in the copy keeps its hue and gets
its lightness mirrored into a dark range (night_rgb), so white fields turn near black, black text near white and the
grey frame dark grey. Paddings, fonts and reliefs are the same in both, so the window keeps its size, and unticking the
box goes back to the untouched theme.

A copy, because a ttk theme made from another one takes over its layouts but none of its style settings. A rule for
the colours instead of a second list of them, because the settings differ between Tk versions and themes (Tk 9 draws
the tick boxes from other options than Tk 8.6) and the rule needs to know none of them.

Three things take no colours from a theme and are painted here: the window's own styles and marks (COLORS: the notice,
errors, warnings, the step marks), Tk's plain widgets (the log, the lists of the drop-down boxes, the window behind the
frames; Night.paint_plain) and Tk's file and folder pickers, which keep their windows between uses and are made anew
after a switch.

Themes that draw with the system's own controls (Windows, macOS) take their colours from the system, not from style
settings: Night.available is false there and the window shows no box. It is also false where the theme is dark
already (X resources can recolour Tk's default theme), since mirroring that would make it light.
"""
import colorsys

try:
    from tkinter import TclError
except ImportError:   # COLORS and night_rgb are data and arithmetic: the tests read them where Python has no Tk
    TclError = Exception

LIGHT, DARK = 'light', 'dark'
NIGHT_THEME = 'totk-night'
NATIVE_THEMES = ('vista', 'xpnative', 'winnative', 'aqua')

# The colours the window sets itself, by mode: the step marks (the keys are guilogic's step states), the notice box,
# error and warning text and the muted version label. Light: the colours the window had before it had a night mode.
# Dark: the same hues, light enough to read on the night theme's frame (tests/installer_gui_test.py holds every text
# colour to a contrast of 4.5 with what it stands on).
COLORS = {
    LIGHT: {'pending': '#808080', 'running': '#1a5fb4', 'done': '#26a269', 'skipped': '#808080', 'failed': '#c01c28',
            'notice_bg': '#fff3cd', 'notice_fg': '#664d03', 'error': '#c01c28', 'muted': '#808080',
            'warning': '#9a6700'},
    DARK: {'pending': '#a8a8a8', 'running': '#78aeed', 'done': '#57e389', 'skipped': '#a8a8a8', 'failed': '#ff8a80',
           'notice_bg': '#5c4a0b', 'notice_fg': '#ffe69c', 'error': '#ff8a80', 'muted': '#a8a8a8',
           'warning': '#f5c211'},
}
# The window's own ttk styles (gui.py App.__init__ makes them) and the COLORS entry each of their options takes.
OWN_STYLES = {'Notice.TLabel': {'background': 'notice_bg', 'foreground': 'notice_fg'},
              'Err.TLabel': {'foreground': 'error'}, 'Version.TLabel': {'foreground': 'muted'}}
# The styles copied where Tk cannot list a theme's styles (8.6): the ones the window uses and the ones Tk's message
# box and file pickers use. Tk 9 lists them itself, these included.
STYLES = ('.', 'TFrame', 'TLabel', 'TButton', 'TEntry', 'TCombobox', 'TCheckbutton', 'TRadiobutton', 'TMenubutton',
          'TScrollbar', 'Vertical.TScrollbar', 'Horizontal.TScrollbar', 'TProgressbar', 'Horizontal.TProgressbar',
          'ComboboxPopdownFrame', 'Toolbutton', 'Treeview', 'Heading', 'Item', 'TSeparator', 'TLabelframe',
          'Title.TLabel', *OWN_STYLES)

# The lightness (HLS, 0 to 1) white and black get in the night theme; every lightness between follows in a straight
# line. The default theme's frame (#d9d9d9) lands at #3b3b3b, its fields at #1c1c1c, its text at #ededed.
NIGHT_OF_WHITE, NIGHT_OF_BLACK = 0.11, 0.93

# Tk's plain widgets: each option of theirs that holds a colour, and the tone of the theme in use it takes in night
# mode (Night.paint_plain). A widget is given the ones it has.
PLAIN = {'background': 'field', 'foreground': 'text', 'insertbackground': 'text', 'selectbackground': 'select',
         'selectforeground': 'selected', 'inactiveselectbackground': 'select', 'highlightbackground': 'frame',
         'highlightcolor': 'text'}
PLAIN_CLASSES = ('Text', 'Listbox')
# Tk's file and folder pickers of the main window (tkfbox.tcl, choosedir.tcl): made once and shown again each time.
PICKERS = ('.__tk_filedialog', '.__tk_choosedir')


def night_rgb(r, g, b):
    """The night colour of r, g, b (0 to 1 each): the same hue and saturation, the lightness mirrored. Light turns
    dark and dark light, and two colours as far apart as text and its background stay about as far apart."""
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return colorsys.hls_to_rgb(h, NIGHT_OF_WHITE + (1 - l) * (NIGHT_OF_BLACK - NIGHT_OF_WHITE), s)


def hex_color(r, g, b):
    return '#%02x%02x%02x' % tuple(round(v * 255) for v in (r, g, b))


def is_color(option):
    """A style option that holds a colour, told by its name: -background, -fieldbackground, -selectforeground,
    -troughcolor, -insertcolor, -bordercolor and the like."""
    name = str(option).lower()
    return 'color' in name or name.endswith(('background', 'foreground'))


class Night:
    """The night mode of one window: Night(root, style) with the theme Tk started with in use, then set(DARK) and
    set(LIGHT). colors is the window's own palette of the mode in use."""

    def __init__(self, root, style):
        self.root, self.style = root, style
        self.base = style.theme_use()
        self.available = self.base not in NATIVE_THEMES and self.frame_is_light()
        self.mode = LIGHT
        self.light = {}   # the light colours of each plain widget painted so far, by window path
        self.paint_own(LIGHT)

    @property
    def colors(self):
        return COLORS[self.mode]

    def set(self, mode):
        if not self.available or mode == self.mode:
            return
        if mode == DARK and NIGHT_THEME not in self.style.theme_names():
            self.build()
        self.style.theme_use(NIGHT_THEME if mode == DARK else self.base)
        self.mode = mode
        for path in PICKERS:   # no picker is open: they are modal, so the box cannot be reached while one is
            if int(self.root.tk.call('winfo', 'exists', path)):
                self.root.tk.call('destroy', path)
        self.paint_plain()

    def frame_is_light(self):
        try:
            r, g, b = self.root.winfo_rgb(str(self.style.lookup('TFrame', 'background')))
        except TclError:
            return True   # no frame colour set: Tk's own is light
        return colorsys.rgb_to_hls(r / 65535, g / 65535, b / 65535)[1] > 0.5

    def paint_own(self, mode):
        """The window's own styles in the theme in use."""
        for name, options in OWN_STYLES.items():
            self.style.configure(name, **{option: COLORS[mode][key] for option, key in options.items()})

    def turned(self, value):
        """value as its night colour; value itself when Tk does not read it as a colour (an empty setting)."""
        try:
            r, g, b = self.root.winfo_rgb(str(value))
        except TclError:
            return value
        return hex_color(*night_rgb(r / 65535, g / 65535, b / 65535))

    def style_names(self):
        """The styles to copy: STYLES and, where Tk can list them (8.7 and later), every style of the base theme."""
        tk, names = self.root.tk, set(STYLES)
        try:
            names.update(str(n) for n in tk.splitlist(tk.call('ttk::style', 'theme', 'styles', self.base)))
        except TclError:
            pass
        return sorted(names)

    def build(self):
        """Makes NIGHT_THEME from the theme in use and leaves it in use. The settings are read and written through
        Tk as they are (fonts and state lists stay Tk's own objects); only colours are replaced."""
        tk = self.root.tk
        copies = []
        for name in self.style_names():
            settings = list(tk.splitlist(tk.call('ttk::style', 'configure', name)))
            for i in range(0, len(settings), 2):
                if is_color(settings[i]):
                    settings[i + 1] = self.turned(settings[i + 1])
            maps = list(tk.splitlist(tk.call('ttk::style', 'map', name)))
            for i in range(0, len(maps), 2):
                if is_color(maps[i]):   # a list of state, value pairs
                    states = list(tk.splitlist(maps[i + 1]))
                    states[1::2] = [self.turned(v) for v in states[1::2]]
                    maps[i + 1] = tuple(states)
            copies.append((name, settings, maps))
        self.style.theme_create(NIGHT_THEME, parent=self.base)
        self.style.theme_use(NIGHT_THEME)
        for name, settings, maps in copies:
            if settings:
                tk.call('ttk::style', 'configure', name, *settings)
            if maps:
                tk.call('ttk::style', 'map', name, *maps)
        self.paint_own(DARK)

    def windows(self, path='.'):
        """The path of every window from path down. A drop-down box makes its list when it first opens: it is made
        here instead, so the list is there to paint."""
        tk = self.root.tk
        if str(tk.call('winfo', 'class', path)) == 'TCombobox':
            try:
                tk.call('ttk::combobox::PopdownWindow', path)
            except TclError:
                pass
        yield path
        for child in tk.splitlist(tk.call('winfo', 'children', path)):
            yield from self.windows(str(child))

    def paint_plain(self):
        """Tk's plain widgets in the mode in use: in night mode the tones of the night theme, in light mode the
        colours each had when it was first painted, which is before any night colour reached it."""
        tk, look = self.root.tk, self.style.lookup
        tones = {'frame': look('TFrame', 'background'), 'field': look('TEntry', 'fieldbackground'),
                 'text': look('TEntry', 'foreground'), 'select': look('TEntry', 'selectbackground', ['focus']),
                 'selected': look('TEntry', 'selectforeground', ['focus'])}
        for path in list(self.windows()):
            if path == '.':
                roles = {'background': 'frame'}
            elif str(tk.call('winfo', 'class', path)) in PLAIN_CLASSES:
                roles = PLAIN
            else:
                continue
            if path not in self.light:
                self.light[path] = {}
                for option in roles:
                    try:
                        self.light[path][option] = tk.call(path, 'cget', '-' + option)
                    except TclError:
                        pass   # a list has no insertion cursor
            for option, light in self.light[path].items():
                tk.call(path, 'configure', '-' + option, tones[roles[option]] if self.mode == DARK else light)
