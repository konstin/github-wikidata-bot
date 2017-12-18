class Error:
	def __init__(self):
		self.errors = {}
		self.infos = {"INF#001":"INFORMATION: Function normalize_url gave an invalid return value.",
		"INF#002":"INFORMATION: Function github_repo_to_api gave an invalid return value.",
		"INF#003":"INFORMATION: Function github_repo_to_api_releases gave an invalid return value.",
		"INF#004":"INFORMATION: Function normalize_version gave an invalid return value."}
	def getErrorString(self, errorCode): return self.errors[errorCode]
	def getInfoString(self, infoCode): return self.infos[infoCode]