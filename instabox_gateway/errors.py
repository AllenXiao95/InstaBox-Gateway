class GatewayError(Exception):
    def __init__(self, code, status=400, *, outcome="FAILED", upstream_status=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.outcome = outcome
        self.upstream_status = upstream_status
        self.request_id = None

    def public(self):
        result = {"code": self.code, "outcome": self.outcome,
                  "upstreamStatus": self.upstream_status}
        if self.request_id:
            result["requestId"] = self.request_id
        return result
