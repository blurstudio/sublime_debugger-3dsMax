
"""

This script creates a connection between the Debugger and 3DS Max for debugging Python 2.

It is inspired by various packages, namely:
    - https://github.com/daveleroy/sublime_debugger
    - https://github.com/cb109/sublime3dsmax

"""

from util import (Queue, log, run, dirname, debugpy_path, join, split,
                  basename, ATTACH_TEMPLATE, ATTACH_ARGS, RUN_TEMPLATE, 
                  INITIALIZE_RESPONSE, TITLE_IDENTIFIER, CONTENT_HEADER,
                  RECORDER_NOT_FOUND, PAUSE_REQUEST )
from interface import DebuggerInterface
from tempfile import gettempdir
import winapi
import socket
import json

interface = None

# The 3DS Max window handle
window = None

processed_seqs = []
run_code = ""

debugpy_send_queue = Queue()
debugpy_socket = None


def main():
    """
    Finds the 3ds Max window, starts the thread to send information to debugger,
    then remains in a loop reading messages from debugger.
    """

    global interface

    find_max_window()

    # Create and start the interface with the debugger
    interface = DebuggerInterface(on_receive=on_receive_from_debugger)
    interface.start()


def on_receive_from_debugger(message):
    """
    Intercept the initialize and attach requests from the debugger
    while debugpy is being set up
    """

    # Load message contents into a dictionary
    contents = json.loads(message)

    log('Received from Debugger:', message)

    # Get the type of command the debugger sent
    cmd = contents['command']
    
    if cmd == 'initialize':
        # Run init request once max connection is established and send success response to the debugger
        interface.send(json.dumps(json.loads(INITIALIZE_RESPONSE)))  # load and dump to remove indents
        processed_seqs.append(contents['seq'])
    
    elif cmd == 'attach':
        # time to attach to Max
        run(attach_to_max, (contents,))

        # Change arguments to valid ones for debugpy
        config = contents['arguments']
        new_args = ATTACH_ARGS.format(
            dir=dirname(config['program']).replace('\\', '\\\\'),
            hostname=config['debugpy']['host'],
            port=int(config['debugpy']['port']),
            filepath=config['program'].replace('\\', '\\\\')
        )

        # Update the message with the new arguments to then be sent to debugpy
        contents = contents.copy()
        contents['arguments'] = json.loads(new_args)
        message = json.dumps(contents)  # update contents to reflect new args

        log("New attach arguments loaded:", new_args)

    # Then just put the message in the debugpy queue
    debugpy_send_queue.put(message)


def find_max_window():
    """
    Finds the open 3DS Max window and keeps a handle to it.

    This function is strongly inspired by the contents of 
    https://github.com/cb109/sublime3dsmax/blob/master/sublime3dsmax.py
    """

    global window

    if window is None:
        # finds the window if it hasn't been found already
        window = winapi.Window.find_window(TITLE_IDENTIFIER)

    if window is None:
        # Raising exceptions shows the text in the Debugger's output.
        # Raise an error to show a potential solution to this problem.
        raise Exception("""
    

                A 3ds Max instance could not be found.
        Please make sure it is open and running, then try again.

        """)

    try:
        # MXS_Scintilla is the identifier for the mini macrorecorder in the bottom left
        window.find_child(text=None, cls="MXS_Scintilla")
    except OSError:
        # Window handle is invalid, 3ds Max has probably been closed.
        # Call this function again and try to find one automatically.
        window = None
        find_max_window()


def send_py_code_to_max(code):
    """
    Sends a command to 3ds Max to run 

    This function is strongly inspired by the contents of 
    https://github.com/cb109/sublime3dsmax/blob/master/sublime3dsmax.py
    """

    try:
        # Make temporary file and set its contents to the code snippet
        filepath = join(gettempdir(), 'temp.py')
        with open(filepath, "w") as f:
            f.write(code)

        # The command to run a python file within Max
        cmd = f'python.ExecuteFile @"{filepath}";'
        log("Sending " + cmd + " to 3ds Max")

        minimacrorecorder = window.find_child(text=None, cls="MXS_Scintilla")

        # If the mini macrorecorder was not found, there is still a chance
        # we are targetting an ancient Max version (e.g. 9) where the
        # listener was not Scintilla based, but instead a rich edit box.
        if minimacrorecorder is None:

            statuspanel = window.find_child(text=None, cls="StatusPanel")
            if statuspanel is None:
                raise Exception(RECORDER_NOT_FOUND)
            
            minimacrorecorder = statuspanel.find_child(text=None, cls="RICHEDIT")
            
            # Verbatim strings (the @ at sign) are not supported in older Max versions.
            cmd = cmd.replace("@", "")
            cmd = cmd.replace("\\", "\\\\")

        if minimacrorecorder is None:
            raise Exception(RECORDER_NOT_FOUND)

        # Encode the command to bytes, send to the mmr, then send 
        # the return key to simulate enter being pressed.
        cmd = cmd.encode("utf-8")  # Needed for ST3!
        minimacrorecorder.send(winapi.WM_SETTEXT, 0, cmd)
        minimacrorecorder.send(winapi.WM_CHAR, winapi.VK_RETURN, 0)
        minimacrorecorder = None
    
    except Exception as e:
        # Raise an error to terminate the adapter
        raise Exception("Could not send code to Max due to error:\n\n" + str(e))


def attach_to_max(contents):
    """
    Defines commands to send to Max, establishes a connection to its commandPort,
    then sends the code to inject debugpy
    """

    global run_code
    config = contents['arguments']

    # Format the simulated attach response to send it back to the debugger
    # while we set up the debugpy in the background
    attach_code = ATTACH_TEMPLATE.format(
        debugpy_path=debugpy_path,
        hostname=config['debugpy']['host'],
        port=int(config['debugpy']['port'])
    )

    # Format RUN_TEMPLATE to point to the temporary
    # file containing the code to run
    run_code = RUN_TEMPLATE.format(
        dir=dirname(config['program']),
        file_name=split(config['program'])[1][:-3] or basename(split(config['program'])[0])[:-3]
    )

    # then send attach code
    log('Sending attach code to Max')
    send_py_code_to_max(attach_code)
    log('Successfully attached to Max')

    # Then start the max debugging threads
    run(start_debugging, ((config['debugpy']['host'], int(config['debugpy']['port'])),))


def start_debugging(address):
    """
    Connects to debugpy in Max, then starts the threads needed to
    send and receive information from it
    """

    log("Connecting to " + address[0] + ":" + str(address[1]))

    # Create the socket used to communicate with debugpy
    global debugpy_socket
    debugpy_socket = socket.create_connection(address)

    log("Successfully connected to Max for debugging. Starting...")

    # Start a thread that sends requests to debugpy
    run(debugpy_send_loop)

    # Make a file to read debugpy's responses line-by-line
    fstream = debugpy_socket.makefile()

    while True:
        try:
            # Wait for the CONTENT_HEADER to show up,
            # then get the length of the content following it
            content_length = 0
            while True:
                header = fstream.readline()
                if header:
                    header = header.strip()
                if not header:
                    break
                if header.startswith(CONTENT_HEADER):
                    content_length = int(header[len(CONTENT_HEADER):])

            # Read the content of the response, then call the callback
            if content_length > 0:
                total_content = ""
                while content_length > 0:
                    content = fstream.read(content_length)
                    content_length -= len(content)
                    total_content += content

                if content_length == 0:
                    message = total_content
                    on_receive_from_debugpy(message)

        except Exception as e:
            # Problem with socket. Close it then return

            log("Failure reading Max's debugpy output: \n" + str(e))
            debugpy_socket.close()
            break


def debugpy_send_loop():
    """
    The loop that waits for items to show in the send queue and prints them.
    Blocks until an item is present
    """

    while True:
        # Get the first message off the queue
        msg = debugpy_send_queue.get()
        if msg is None:
            # get() is blocking, so None means it was intentionally
            # added to the queue to stop this loop, or that a problem occurred
            return
        else:
            try:
                # First send the content header with the length of the message, then send the message
                debugpy_socket.send(bytes(CONTENT_HEADER + '{}\r\n\r\n'.format(len(msg)), 'UTF-8'))
                debugpy_socket.send(bytes(msg, 'UTF-8'))
                log('Sent to debugpy:', msg)
            except OSError:
                log("Debug socket closed.")
                return


def on_receive_from_debugpy(message):
    """
    Handles messages going from debugpy to the debugger
    """

    # Load the message into a dictionary
    c = json.loads(message)
    seq = int(c.get('request_seq', -1))  # a negative seq will never occur
    cmd = c.get('command', '')

    if cmd == 'configurationDone':
        # When Debugger & debugpy are done setting up, send the code to debug
        send_py_code_to_max(run_code)

    # Send responses and events to debugger
    if seq in processed_seqs:
        # Should only be the initialization request
        log("Already processed, debugpy response is:", message)
    else:
        # Send the message normally to the debugger
        log('Received from debugpy:', message)
        interface.send(message)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(str(e))
        raise e
