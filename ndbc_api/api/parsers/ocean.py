from typing import List

import pandas as pd

from api.parsers._base import BaseParser


class OceanParser(BaseParser):

    INDEX_COL = 0

    @classmethod
    def df_from_responses(cls, responses: List[dict]) -> pd.DataFrame:
        return super(OceanParser, cls).df_from_responses(responses)