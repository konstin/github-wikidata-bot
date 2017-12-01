from main import *

class Test:
	def __init__(self):
		self.errorLog = []
		self.infoLog = []
		self.error = Error()
	def __str__(self):
		errorLog = ""
		infoLog = ""
		for error in self.errorLog:
			errorLog += self.error.getErrorString(error) + "\n"
		for info in self.infoLog:
			infoLog += self.error.getInfoString(info)+ "\n"
		return errorLog + "\n" + infoLog
	def test(self):
		if normalize_url("/hallo.py.git") != "hallo.py":
			self.infoLog.append("INF#001")
		if github_repo_to_api("https://github.com/mitmirzutun") != "https://api.github.com/repos/mitmirzutun":
			self.infoLog.append("INF#002")
		if github_repo_to_api_releases("https://github.com/mitmirzutun") != "https://api.github.com/repos/mitmirzutun/releases":
			self.infoLog.append("INF#003")
	def isEmpty(self): return self.infoLog == [] and self.errorLog == []

class Error:
	def __init__(self):
		self.errors = {}
		self.infos = {"INF#001":"INFORMATION: Function normalize_url gave an invalid return value.",
		"INF#002":"INFORMATION: Function github_repo_to_api gave an invalid return value.",
		"INF#003":"INFORMATION: Function github_repo_to_api_releases gave an invalid return value."}
	def getErrorString(self, errorCode): return self.errors[errorCode]
	def getInfoString(self, infoCode): return self.infos[infoCode]

def runTest(run = "run"):
	if run == "run":
		print("Starting Test run.")
		test1 = Test()
		test1.test()
		print("Test run finished.")
		if not test1.isEmpty():
			print("Following Problems occured:\n"+str(test1))
	elif run == "surpress":
		print("Test run surpressed.")

if __name__ == "__main__":
	runTest("surpress")