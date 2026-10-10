class RunService:
    def create_run(self, req):
        raise NotImplementedError

    def finish_brief(self, run_id):
        raise NotImplementedError

    def get_run(self, run_id):
        raise NotImplementedError
