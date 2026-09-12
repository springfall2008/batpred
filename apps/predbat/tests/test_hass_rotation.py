import os
import tempfile
import threading
from unittest.mock import patch


def test_hass_rotation(my_predbat):
    """Tests log rotation in the Hass class."""
    failed = False
    print("**** test_hass_rotation ****")

    class MockHass:
        def __init__(self, logfile):
            self.logfile = logfile
            self.args = {"pred_bat": {}}

        from hass import Hass

        _rotate_log_if_needed = Hass._rotate_log_if_needed

    with tempfile.TemporaryDirectory() as root:
        # Save cwd and restore it later to allow tempdir cleanup
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            # Test early returns
            with open("predbat.log", "w", encoding="utf-8") as f:
                hass_obj = MockHass(f)
                hass_obj._rotate_log_if_needed(max_logs=2)
                if os.path.exists("predbat.1.log"):
                    print("ERROR: log rotated when below threshold")
                    failed = True
            if hasattr(hass_obj, "logfile") and not hass_obj.logfile.closed:
                hass_obj.logfile.close()

            with open("predbat.log", "w", encoding="utf-8") as f:
                hass_obj = MockHass(f)
                with patch("threading.current_thread") as mock_thread:
                    mock_thread.return_value = threading.Thread()
                    hass_obj.logfile.tell = lambda: 10000001
                    hass_obj._rotate_log_if_needed(max_logs=2)
                    if os.path.exists("predbat.1.log"):
                        print("ERROR: log rotated on non-main thread")
                        failed = True
            if hasattr(hass_obj, "logfile") and not hass_obj.logfile.closed:
                hass_obj.logfile.close()

            with open("predbat.log", "w", encoding="utf-8") as f:
                f.write("old log content")
            with open("predbat.1.log", "w", encoding="utf-8") as f:
                f.write("very old log content")

            with open("predbat.log", "a", encoding="utf-8") as f:
                hass_obj = MockHass(f)
                with patch("threading.current_thread", return_value=threading.main_thread()):
                    hass_obj.logfile.tell = lambda: 10000001
                    hass_obj._rotate_log_if_needed(max_logs=2)

                    if hass_obj.logfile.closed:
                        print("ERROR: logfile left closed")
                        failed = True

            if hasattr(hass_obj, "logfile") and not hass_obj.logfile.closed:
                hass_obj.logfile.close()
            with open("predbat.1.log", "r", encoding="utf-8") as r:
                content = r.read()
                if content != "old log content":
                    print("ERROR: predbat.1.log does not hold old content, got: " + content)
                    failed = True

            with open("predbat.log", "a", encoding="utf-8") as f:
                hass_obj = MockHass(f)
                with patch("threading.current_thread", return_value=threading.main_thread()):
                    hass_obj.logfile.tell = lambda: 10000001
                    original_open = __builtins__["open"]

                    def mock_open(*args, **kwargs):
                        if args[0] == "predbat.log" and args[1] == "a":
                            raise OSError("mock error")
                        return original_open(*args, **kwargs)

                    with patch("builtins.open", mock_open):
                        hass_obj._rotate_log_if_needed(max_logs=2)

                        if hass_obj.logfile.closed:
                            print("ERROR: logfile left closed after reopen failure")
                            failed = True
            if hasattr(hass_obj, "logfile") and not hass_obj.logfile.closed:
                hass_obj.logfile.close()
        finally:
            os.chdir(old_cwd)

    print("**** test_hass_rotation PASSED ****" if not failed else "**** test_hass_rotation FAILED ****")
    return failed
