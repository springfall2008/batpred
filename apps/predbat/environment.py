def is_package_installed(package_name: str) -> bool:
    try:
        import importlib

        module = importlib.import_module(package_name)
        return True
    except ImportError:
        return False


def is_jinja2_installed() -> bool:
    return is_package_installed("jinja2")


def is_appdaemon_environment() -> bool:
    if is_package_installed("appdaemon"):
        return True
    else:
        return False
