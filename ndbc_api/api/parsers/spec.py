from datetime import datetime
from typing import List

from api.requests._base import BaseRequest


class Spec(BaseRequest):

    FORMAT = 'spec'
    FILE_FORMAT = '.spec'

    @classmethod
    def build_request(
        cls,
        station_id: str,
        start_time: datetime,
        end_time: datetime
        ) -> List[str]:
        return super(Spec, cls).build_request(station_id, start_time, end_time)
