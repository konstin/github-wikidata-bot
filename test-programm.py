from main import *
from error import *



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
		testLinkFunctions(self)
	def isEmpty(self): return self.infoLog == [] and self.errorLog == []

def testLinkFunctions(test_function):
	if normalize_url("/hallo.py.git") != "hallo.py":
		test_function.infoLog.append("INF#001")
	if github_repo_to_api("https://github.com/mitmirzutun") != "https://api.github.com/repos/mitmirzutun":
		test_function.infoLog.append("INF#002")
	if github_repo_to_api_releases("https://github.com/mitmirzutun") != "https://api.github.com/repos/mitmirzutun/releases":
		test_function.infoLog.append("INF#003")

def testNormalizeVersions(test_function):
	if normalize_version("","") != "" or normalize_version("v1.-1") != "1.1":
		test_function.infoLog.append("INF#004")

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
	runTest()