from dataclasses import dataclass

from tests.fixtures import models


@dataclass
class Shipment:
    destinations: list[models.Address]
