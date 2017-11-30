from main import *


class Test:
	def __init__(self):
		self.errorLog = []
	def run(self):
		if github_repo_to_api("https://github.com/hallon") != "https://api.github.com/repos/hallo":
			self.errorLog.append("ERR#001")
	def printErrorLog(self):
		Error1 = Error()
		for i in self.errorLog:
			Error1.raiseError(i)
class Error:
	def __init__(self):
		self.errors = {"ERR#001":"Error: github_repo_to_api gave an ivalid return value."}
	def raiseError(self, error):
		print(self.errors[error])
if __name__ == "__main__":
	Test1 = Test()
	Test1.run()
	Test1.printErrorLog()