"""Thunar: explicit snapshot of the observed directory."""
APP_CLASS = 'Thunar'
COMMAND = 'thunar'
DIRECTORY = True


def export(window, tree, home):
    from . import saved_path
    return saved_path(window, tree, home, directory=True)


def verify(window, tree, path, home=None):
    from . import visible
    return window.get('class') == APP_CLASS and visible(window, tree, path, home)
