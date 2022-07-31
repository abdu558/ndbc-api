from datetime import datetime
from typing import List

from api.requests._base import BaseRequest


class CwindRequest(BaseRequest):

    FORMAT = 'cwind'
    FILE_FORMAT = '.cwind'

    @classmethod
    def build_request(
        cls,
        station_id: str,
        start_time: datetime,
        end_time: datetime
        ) -> List[str]:
        return super(CwindRequest, cls).build_request(station_id, start_time, end_time)