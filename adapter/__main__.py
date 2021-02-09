
"""

This script creates a connection between the Debugger and 3DS Max for debugging Python 2.

It is inspired by various packages, namely:
    - https://github.com/daveleroy/sublime_debugger
    - https://github.com/cb109/sublime3dsmax

"""

from tempfile import gettempdir
from interface import debugger_queue, start_response_thread, read_debugger_input
from queue import Queue
from util import *
import winapi
import socket
import json
import time
import sys
import os


# The 3DS Max window handle
window = None

signal_location = join(dirname(abspath(__file__)), 'finished.txt')
last_seq = -1

processed_seqs = []
run_code = ""

ptvsd_send_queue = Queue()
ptvsd_socket: socket.socket


# Avoiding stalls
inv_seq = 9223372036854775806  # The maximum int value in Python 2, -1  (hopefully never gets reached)
artificial_seqs = []  # keeps track of which seqs we have sent
waiting_for_pause_event = False

avoiding_continue_stall = False
stashed_event = None

disconnecting = False


def main():
    """
    Finds the 3ds Max window, starts the thread to send information to debugger,
    then remains in a loop reading messages from debugger.
    """

    find_max_window()

    if os.path.exists(signal_location):
        os.remove(signal_location)

    start_response_thread()

    read_debugger_input(on_receive_from_debugger)


def on_receive_from_debugger(message):
    """
    Intercept the initialize and attach requests from the debugger
    while ptvsd is being set up
    """

    global last_seq, avoiding_continue_stall

    contents = json.loads(message)
    last_seq = contents.get('seq')

    log('Received from Debugger:', message)

    cmd = contents['command']
    
    if cmd == 'initialize':
        # Run init request once max connection is established and send success response to the debugger
        debugger_queue.put(json.dumps(json.loads(INITIALIZE_RESPONSE)))  # load and dump to remove indents
        processed_seqs.append(contents['seq'])
        pass
    
    elif cmd == 'attach':
        # time to attach to max
        run(attach_to_max, (contents,))

        # Change arguments to valid ones for ptvsd
        config = contents['arguments']
        new_args = ATTACH_ARGS.format(
            dir=dirname(config['program']).replace('\\', '\\\\'),
            hostname=config['ptvsd']['host'],
            port=int(config['ptvsd']['port']),
            filepath=config['program'].replace('\\', '\\\\')
        )

        contents = contents.copy()
        contents['arguments'] = json.loads(new_args)
        message = json.dumps(contents)  # update contents to reflect new args

        log("New attach arguments loaded:", new_args)
    
    elif cmd == 'continue':
        avoiding_continue_stall = True

    # Then just put the message in the ptvsd queue
    ptvsd_send_queue.put(message)


def find_max_window():
    """
    Finds the open 3DS Max window and keeps a handle to it.

    This function is strongly inspired by the contents of 
    https://github.com/cb109/sublime3dsmax/blob/master/sublime3dsmax.py
    """

    global window

    if window is None:
        window = winapi.Window.find_window(TITLE_IDENTIFIER)

    if window is None:
        raise Exception("""
    

        An Autodesk 3ds Max instance could not be found.
        Please make sure it is open and running, then try again.

        """)

    try:
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

        cmd = cmd.encode("utf-8")  # Needed for ST3!
        minimacrorecorder.send(winapi.WM_SETTEXT, 0, cmd)
        minimacrorecorder.send(winapi.WM_CHAR, winapi.VK_RETURN, 0)
        minimacrorecorder = None
    
    except Exception as e:

        raise Exception("Could not send vital code to Max due to error:\n\n" + str(e))


def attach_to_max(contents: dict):
    """
    Defines commands to send to Max, establishes a connection to its commandPort,
    then sends the code to inject ptvsd
    """

    global run_code
    config = contents['arguments']

    attach_code = ATTACH_TEMPLATE.format(
        ptvsd_path=ptvsd_path,
        hostname=config['ptvsd']['host'],
        port=int(config['ptvsd']['port'])
    )

    run_code = RUN_TEMPLATE.format(
        dir=dirname(config['program']),
        file_name=split(config['program'])[1][:-3] or basename(split(config['program'])[0])[:-3],
        signal_location=signal_location.replace('\\', '\\\\')
    )

    # then send attach code
    log('Sending attach code to Max')
    send_py_code_to_max(attach_code)

    log('Successfully attached to Max')

    # Then start the max debugging threads
    run(start_debugging, ((config['ptvsd']['host'], int(config['ptvsd']['port'])),))

    # And finally wait for the signal from ptvsd that debugging is done
    run(wait_for_signal)


def wait_for_signal():
    """
    Waits for the signal location to exist, which means debugging is done.
    Deletes the signal location and prepares this adapter for disconnect.
    """

    global disconnecting

    while True:
        
        if os.path.exists(signal_location):
            log('--- FINISHED DEBUGGING ---')

            os.remove(signal_location)
            run(disconnect)


def start_debugging(address):
    """
    Connects to ptvsd in Max, then starts the threads needed to
    send and receive information from it
    """

    log("Connecting to " + address[0] + ":" + str(address[1]))

    global ptvsd_socket
    ptvsd_socket = socket.create_connection(address)

    log("Successfully connected to Max for debugging. Starting...")

    run(ptvsd_send_loop)  # Start sending requests to ptvsd

    fstream = ptvsd_socket.makefile()

    while True:
        try:
            content_length = 0
            while True:
                header = fstream.readline()
                if header:
                    header = header.strip()
                if not header:
                    break
                if header.startswith(CONTENT_HEADER):
                    content_length = int(header[len(CONTENT_HEADER):])

            if content_length > 0:
                total_content = ""
                while content_length > 0:
                    content = fstream.read(content_length)
                    content_length -= len(content)
                    total_content += content

                if content_length == 0:
                    message = total_content
                    on_receive_from_ptvsd(message)

        except Exception as e:
            log("Failure reading Max's ptvsd output: \n" + str(e))
            ptvsd_socket.close()
            break


def ptvsd_send_loop():
    """
    The loop that waits for items to show in the send queue and prints them.
    Blocks until an item is present
    """

    while True:
        msg = ptvsd_send_queue.get()
        if msg is None:
            return
        else:
            try:
                ptvsd_socket.send(bytes('Content-Length: {}\r\n\r\n'.format(len(msg)), 'UTF-8'))
                ptvsd_socket.send(bytes(msg, 'UTF-8'))
                log('Sent to ptvsd:', msg)
            except OSError:
                log("Debug socket closed.")
                return


def on_receive_from_ptvsd(message):
    """
    Handles messages going from ptvsd to the debugger
    """

    global inv_seq, artificial_seqs, waiting_for_pause_event, avoiding_continue_stall, stashed_event

    c = json.loads(message)
    seq = int(c.get('request_seq', -1))  # a negative seq will never occur
    cmd = c.get('command', '')

    if cmd == 'configurationDone':
        # When Debugger & ptvsd are done setting up, send the code to debug
        send_py_code_to_max(run_code)
    
    elif cmd == "variables":
        # Hide the __builtins__ variable (causes errors in the debugger gui)
        vars = c['body'].get('variables')
        if vars:
            toremove = []
            for var in vars:
                if var['name'] in ('__builtins__', '__doc__', '__file__', '__name__', '__package__'):
                    toremove.append(var)
            for var in toremove:
                vars.remove(var)
            message = json.dumps(c)
    
    elif c.get('event', '') == 'stopped' and c['body'].get('reason', '') == 'step':
        # Sometimes (often) ptvsd stops on steps, for an unknown reason.
        # Respond to this with a forced pause to put things back on track.
        log("Stall detected. Sending unblocking command to ptvsd.")
        req = PAUSE_REQUEST.format(seq=inv_seq)
        ptvsd_send_queue.put(req)
        artificial_seqs.append(inv_seq)
        inv_seq -= 1

        # We don't want the debugger to know ptvsd stalled, so pretend it didn't.
        return
    
    elif seq in artificial_seqs:
        # Check for success, then do nothing and wait for pause event to show up
        if c.get('success', False): 
            waiting_for_pause_event = True
        else:
            log("Stall could not be recovered.")
        return
        
    elif waiting_for_pause_event and c.get('event', '') == 'stopped' and c['body'].get('reason', '') == 'pause':
        # Set waiting for pause event to false and change the reason for the stop to be a step. 
        # Debugging can operate normally again
        waiting_for_pause_event = False
        c['body']['reason'] = 'step'
        message = json.dumps(c)
    
    elif avoiding_continue_stall and c.get('event', '') == 'stopped' and c['body'].get('reason', '') == 'breakpoint':
        # temporarily hold this message to send only after the continued event is received
        log("Temporarily stashed: ", message)
        stashed_event = message
        return
    
    elif avoiding_continue_stall and c.get('event', '') == 'continued':
        avoiding_continue_stall = False

        if stashed_event:
            log('Received from ptvsd:', message)
            debugger_queue.put(message)

            log('Sending stashed message:', stashed_event)
            debugger_queue.put(stashed_event)

            stashed_event = None
            return

    # Send responses and events to debugger
    if seq in processed_seqs:
        # Should only be the initialization request
        log("Already processed, ptvsd response is:", message)
    else:
        log('Received from ptvsd:', message)
        debugger_queue.put(message)


def disconnect():
    """
    Clean things up by unblocking (and killing) all threads, then exit
    """

    # Unblock and kill the send threads
    debugger_queue.put(None)
    while debugger_queue.qsize() != 0:
        time.sleep(0.1)
    
    ptvsd_send_queue.put(None)
    while ptvsd_send_queue.qsize() != 0:
        time.sleep(0.1)

    # Close ptvsd socket and stdin so readline() functions unblock
    ptvsd_socket.close()
    sys.stdin.close()

    # exit all threads
    os._exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(str(e))
        raise e
