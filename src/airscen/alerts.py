class AlertEngine:
    def evaluate(self, company, run, previous):
        raise NotImplementedError

    def deliver(self, alert):
        raise NotImplementedError
