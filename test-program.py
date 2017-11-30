from main import *


class Test:
	def __init__(self):
		self.errorLog = []
	def run(self):
		if github_repo_to_api("https://github.com/hallon") != "https://api.github.com/repos/hallo":
			self.errorLog.append("ERR: github_repo_to_api")
	def printErrorLog(self):
		for i in self.errorLog:
			print(i)

if __name__ == "__main__":
	Test1 = Test()
	Test1.run()
	Test1.printErrorLog()