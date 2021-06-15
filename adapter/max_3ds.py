
from Debugger.modules.typecheck import *
import Debugger.modules.debugger.adapter as adapter

from shutil import which
import socket

from os.path import dirname, split, join
from .util import (debugpy_path, ATTACH_TEMPLATE, RUN_TEMPLATE, RECORDER_NOT_FOUND,
					EXEC_COMMAND, TITLE_IDENTIFIER, log as custom_log)
from tempfile import gettempdir

import threading
import sublime
import winapi


# This is the id of your adapter. It must be unique and match no 
# other existing adapters.
adapter_type = "3dsMax"


class Max(adapter.AdapterConfiguration):

	def __init__(self) -> None:
		super().__init__()
		self.maxWindow = None

	@property
	def type(self): return adapter_type

	async def start(self, log, configuration):
		"""
		start() is called when the play button is pressed in the debugger.
		
		The configuration is passed in, allowing you to get necessary settings
		to use when setting up the adapter as it starts up (such as getting the 
		desired host/port to connect to, show below)

		The configuration will be chosen by the user from the 
		configuration_snippets function below, and its contents are the contents 
		of "body:". However, the user can change the configurations manually so 
		make sure to account for unexpected changes. 
		"""

		# Start by finding the python installation on the system
		python = configuration.get("pythonPath")

		if not python:
			if which("python3"):
				python = "python3"
			elif not (python := which("python")):
				raise Exception('No python installation found')
		
		custom_log(f"Found python install: {python}")
		
		# Get debugpy host/port from config
		host = configuration['host']
		if host == 'localhost':
			host = '127.0.0.1'
		port = int(configuration['port'])
		
		# Format ATTACH_TEMPLATE to set up debugpy in the background
		attach_code = ATTACH_TEMPLATE.format(
			debugpy_path=debugpy_path,
			hostname=host,
			port=port,
			interpreter=python,
		)
		
 		self.send_py_code_to_max(attach_code)

		# Format RUN_TEMPLATE to point to the file containing the code to run
		run_code = RUN_TEMPLATE.format(
			dir=dirname(configuration['program']),
			file_name=split(configuration['program'])[1][:-3] or basename(split(configuration['program'])[0])[:-3]
		)

		# Set up timer to send the run code 1 sec after establishing the connection with debugpy
		threading.Timer(1, self.send_py_code_to_max, args=(run_code,))
		
		# Start the transport
		return adapter.SocketTransport(log, host, port)

	def find_max_window(self):
		"""
		Finds the open 3DS Max window and keeps a handle to it.

		This function is strongly inspired by the contents of 
		https://github.com/cb109/sublime3dsmax/blob/master/sublime3dsmax.py
		"""

		if self.maxWindow is None:
			# finds the window if it hasn't been found already
			self.maxWindow = winapi.Window.find_window(TITLE_IDENTIFIER)

		if self.maxWindow is None:
			# Raising exceptions shows the text in the Debugger's output.
			# Raise an error to show a potential solution to this problem.
			raise Exception("""
		

					A 3ds Max instance could not be found.
			Please make sure it is open and running, then try again.

			""")

		try:
			# MXS_Scintilla is the identifier for the mini macrorecorder in the bottom left
			self.maxWindow.find_child(text=None, cls="MXS_Scintilla")
		except OSError:
			# Window handle is invalid, 3ds Max has probably been closed.
			# Call this function again and try to find one automatically.
			self.maxWindow = None
			self.find_max_window()
	
	def send_py_code_to_max(self, code):
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

			minimacrorecorder = self.maxWindow.find_child(text=None, cls="MXS_Scintilla")

			# If the mini macrorecorder was not found, there is still a chance
			# we are targetting an ancient Max version (e.g. 9) where the
			# listener was not Scintilla based, but instead a rich edit box.
			if minimacrorecorder is None:

				statuspanel = self.maxWindow.find_child(text=None, cls="StatusPanel")
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

	async def install(self, log):
		"""
		When someone installs your adapter, they will also have to install it 
		through the debugger itself. That is when this function is called. It
		allows you to download any extra files or resources, or install items
		to other parts of the device to prepare for debugging in the future
		"""
		
		# Nothing to do when installing, just return
		pass

	@property
	def installed_version(self) -> Optional[str]:
		# The version is only used for display in the UI
		return '0.0.1'

	@property
	def configuration_snippets(self) -> Optional[list]:
		"""
		You can have several configurations here depending on your adapter's 
		offered functionalities, but they all need a "label", "description", 
		and "body"
		"""

		return [
			{
				"label": "3DS Max: Python 2 Debugging",
				"description": "Run and Debug Python 2 code in 3DS Max",
				"body": {
					"name": "3DS Max: Python 2 Debugging",  
					"type": adapter_type,
					"program": "\${file\}",
					"request": "attach",  # can only be attach or launch
					"host": "localhost",
					"port": 7003,
				}
			},
		]

	@property
	def configuration_schema(self) -> Optional[dict]:
		"""
		I am not completely sure what this function is used for. However, 
		it must be present.
		"""

		return None

	async def configuration_resolve(self, configuration):
		"""
		In this function, you can take a currently existing configuration and 
		resolve various variables in it before it gets passed to start().

		Therefore, configurations where values are stated as {my_var} can 
		then be filled out before being used to start the adapter.
		"""

		return configuration
