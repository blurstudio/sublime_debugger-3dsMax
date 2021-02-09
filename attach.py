
"""

This script adds the the containing package as a valid debug adapter in the Debugger's settings

"""

from Debugger.modules.debugger.debugger import Debugger
from os.path import join, abspath, dirname
from threading import Timer
import sublime
import time


adapter_type = "3DSMPy2"  # NOTE: type name must be unique to each adapter
package_path = dirname(abspath(__file__))
adapter_path = join(package_path, "adapter")


# The version is only used for display in the GUI
version = "1.0"

# You can have several configurations here depending on your adapter's offered functionalities,
# but they all need a "label", "description", and "body"
config_snippets = [
    {
        "label": "3DS Max: Python 2 Debugging",
        "description": "Run and Debug Python 2 code in 3DS Max",
        "body": {
            "name": "3DS Max: Python 2 Debugging",  
            "type": adapter_type,
            "program": "\${file\}",
            "request": "attach",  # can only be attach or launch
            "ptvsd":  # The host/port used to communicate with ptvsd in maya
            {
                "host": "localhost",
                "port": 7003
            },
        }
    },
]

# The settings used by the Debugger to run the adapter.
settings = {
    "type": adapter_type,
    "command": ["python", adapter_path]
}

# Instantiate variables needed for checking thread
running = False
check_speed = 5  # number of seconds to wait between checks for adapter presence in debugger instances


def check_for_adapter():
    """
    Gets run in a thread to inject configuration snippets and version information 
    into adapter objects of type adapter_type in each debugger instance found
    """

    while running:

        for instance in Debugger.instances.values():
            adapter = getattr(instance, "adapters", {}).get(adapter_type, None)
            
            if adapter and not adapter.version:
                adapter.version = version
                adapter.snippets = config_snippets
        
        time.sleep(check_speed)


def plugin_loaded():
    """ Add adapter to debugger settings for it to be recognized """

    # Add entry to debugger settings
    debugger_settings = sublime.load_settings('debugger.sublime-settings')
    adapters_custom = debugger_settings.get('adapters_custom', {})

    adapters_custom[adapter_type] = settings

    debugger_settings.set('adapters_custom', adapters_custom)
    sublime.save_settings('debugger.sublime-settings')

    # Start checking thread
    global running, timer
    running = True
    Timer(1, check_for_adapter).start()


def plugin_unloaded():
    """ This is done every unload just in case this adapter is being uninstalled """

    # Wait for checking thread to finish
    global running
    running = False
    time.sleep(check_speed + .1)

    # Remove entry from debugger settings
    debugger_settings = sublime.load_settings('debugger.sublime-settings')
    adapters_custom = debugger_settings.get('adapters_custom', {})

    adapters_custom.pop(adapter_type, "")

    debugger_settings.set('adapters_custom', adapters_custom)
    sublime.save_settings('debugger.sublime-settings')
