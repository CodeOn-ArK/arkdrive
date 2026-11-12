"""GNOME Text Editor: only saved files with an observable path."""
# app_class is the observed native WM_CLASS, not the desktop-file id.
APP_CLASS = 'gnome-text-editor'
COMMAND = 'gnome-text-editor'
DIRECTORY = False


def export(window, tree, home):
    from . import saved_path
    return saved_path(window, tree, home)


def verify(window, tree, path, home=None):
    from . import visible
    return window.get('class') == APP_CLASS and visible(window, tree, path, home)
