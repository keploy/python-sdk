from typing import Literal
from keploy.constants import MODE_OFF, MODE_RECORD, MODE_TEST


mode = MODE_RECORD

def isValidMode(mode):
	if mode in [MODE_OFF, MODE_RECORD, MODE_TEST]:
		return True
	return False

def getMode():
	return mode

MODES = Literal["off", "record", "test"]
def setMode(m:MODES):
	if isValidMode(m):
		global mode
		mode = m
		return
	raise Exception("Mode:{} not supported by keploy. Please enter a valid mode.".format(m))
