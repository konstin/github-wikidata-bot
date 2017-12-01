from main import *

class Test:
	def __init__(self):
		self.errorLog = []
		self.infoLog = []
		self.error = Error()
	def __str__(self):
		errorLog = ""
		infoLog = ""
		for i in self.errorLog:
			errorLog += self.error.getErrorString(i) + "\n"
		for i in self.infoLog:
			infoLog += self.error.getInfoString(i)+ "\n"
		return errorLog + "\n" + infoLog
	def test(self):
		if github_repo_to_api("https://github.com/mitmirzutun") != "https://api.github.com/repos/mitmirzutun":
			self.infoLog.append("INF#001")
		if github_repo_to_api_releases("https://github.com/mitmirzutun") != "https://api.github.com/repos/mitmirzutun/releases":
			self.infoLog.append("INF#002")

class Error:
	def __init__(self):
		self.errors = {}
		self.infos = {"INF#001":"INFORMATION: Function github_repo_to_api gave an invalid return value.",
		"INF#002":"INFORMATION: Function github_repo_to_api_releases gave an invalid return value."}
	def getErrorString(self, errorCode): return self.errors[errorCode]
	def getInfoString(self, infoCode): return self.infos[infoCode]

if __name__ == "__main__":
	test1 = Test()
	test1.test()
	print(str(test1))