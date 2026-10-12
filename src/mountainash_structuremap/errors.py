class StructureMapError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        owner: str = "mapping restriction",
        remedy: str,
        table: str | None = None,
        row: int | None = None,
        column: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.owner = owner
        self.remedy = remedy
        self.table = table
        self.row = row
        self.column = column
        context = ", ".join(
            part
            for part in (
                f"path={path}" if path else "",
                f"table={table}" if table else "",
                f"row={row}" if row is not None else "",
                f"column={column}" if column else "",
            )
            if part
        )
        super().__init__(f"{code}: {message}" + (f" ({context})" if context else "") + f"; remedy: {remedy}")
