"""GNOME Text Editor: only saved files with an observable path."""
APP_CLASS = 'org.gnome.TextEditor'
COMMAND = 'gnome-text-editor'
DIRECTORY = False


def export(window, tree, home):
    from . import saved_path
    return saved_path(window, tree, home)


def verify(window, tree, path):
    from . import visible
    return window.get('class') == APP_CLASS and visible(window, tree, path)
