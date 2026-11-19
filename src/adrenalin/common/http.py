class Blocked(Exception):
    def __init__(self, host: str, until: float):
        super().__init__(f"{host} blocked until {until}")
        self.host = host
        self.until = until
