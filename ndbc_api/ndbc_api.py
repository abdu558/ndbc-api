"""An API for retrieving data from the NDBC.

This module defines the `NdbcApi`, the top-level object which creates, handles,
caches, parses, and returns NDBC data.

Example:
    ```python3
    from ndbc_api import NdbcApi
    api = NdbcApi()
    available_stations = api.stations()
    modes = api.get_modes()
    df_stdmet_tplm2 = api.get_data(
        'tplm2',
        'stdmet',
        '2020-01-01',
        '2022-01-01',
        as_df=True
    )
    ```

Attributes:
    log (:obj:`logging.Logger`): The logger at which to register HTTP
        request and response status codes and headers used for debug
        purposes.
    headers(:dict:): The request headers for use in the NDBC API's request
        handler.
"""
import logging
import itertools
import pickle
import warnings
from datetime import datetime, timedelta
from typing import Any, List, Tuple, Union

import pandas as pd

from .api.handlers.data import DataHandler
from .api.handlers.stations import StationsHandler
from .config import (DEFAULT_CACHE_LIMIT, HTTP_BACKOFF_FACTOR, HTTP_DEBUG,
                     HTTP_DELAY, HTTP_RETRY, LOGGER_NAME, VERIFY_HTTPS)
from .exceptions import (HandlerException, ParserException, RequestException,
                         ResponseException, TimestampException)
from .utilities.req_handler import RequestHandler
from .utilities.singleton import Singleton
from concurrent.futures import ThreadPoolExecutor


class NdbcApi(metaclass=Singleton):
    """An API for querying the National Data Buoy Center.

    The `NdbcApi` is metaclassed as a singleton to conserve NDBC resources. It
    uses two private handlers to build requests and parse responses to the NDBC
    over HTTP(s). It also uses a LRU-cached request handler to execute requests
    against the NDBC, logging response statuses as they are executed.

    Attributes:
        logging_level: The `logging.Logger`s log level, 1 if the `debug`
            flag is set in the `__init__` method, and 0 otherwise.
        cache_limit: The handler's global limit for caching
            `NdbcApi` responses. This is implemented as a least-recently
            used cache, designed to conserve NDBC resources when querying
            measurements for a given station over similar time ranges.
        delay: The HTTP(s) request delay parameter, in seconds.
        retries: = The number of times to retry a request to the NDBC data
            service.
        backoff_factor: The back-off parameter, used in conjunction with
            `retries` to re-attempt requests to the NDBC data service.
        verify_https: A flag which indicates whether to attempt requests to the
            NDBC data service over HTTP or HTTPS.
        debug: A flag for verbose logging and response-level status reporting.
            Affects the instance's `logging.Logger` and the behavior of its
            private `RequestHandler` instance.
    """

    log = logging.getLogger(LOGGER_NAME)
    warnings.simplefilter(action='ignore', category=FutureWarning)

    def __init__(
        self,
        logging_level: int = logging.WARNING if HTTP_DEBUG else 0,
        cache_limit: int = DEFAULT_CACHE_LIMIT,
        headers: dict = {},
        delay: int = HTTP_DELAY,
        retries: int = HTTP_RETRY,
        backoff_factor: float = HTTP_BACKOFF_FACTOR,
        verify_https: bool = VERIFY_HTTPS,
        debug: bool = HTTP_DEBUG,
    ):
        """Initializes the singleton `NdbcApi`, sets associated handlers."""
        self.log.setLevel(logging_level)
        self.cache_limit = cache_limit
        self.headers = headers
        self._handler = self._get_request_handler(
            cache_limit=self.cache_limit,
            delay=delay,
            retries=retries,
            backoff_factor=backoff_factor,
            headers=self.headers,
            debug=debug,
            verify_https=verify_https,
        )
        self._stations_api = StationsHandler
        self._data_api = DataHandler

    def dump_cache(self, dest_fp: Union[str, None] = None) -> Union[dict, None]:
        """Dump the request cache to dict or the specified filepath.

        Dump the request, response pairs stored in the `NdbcApi`'s
        `Request_handler` as a `dict`, either returning the object, if no
        `dest_fp` is specified, or serializing (pickling) the object and writing
        it to the specified `dest_fp`.

        Args:
            dest_fp: The destination filepath for the serialized `RequestsCache`
                contents.

        Returns:
            The cached request, response pairs as a `dict`, or `None` if a
            `dest_fp` is specified when calling the method.
        """
        data = dict()
        ids = [r.id_ for r in self._handler.stations]
        caches = [r.reqs.cache for r in self._handler.stations]
        if ids:
            for station_id, cache in zip(ids, caches):
                data[station_id] = dict()
                reqs = cache.keys()
                for req in reqs:
                    resp = cache[req].v
                    data[station_id][req] = resp
        if dest_fp:
            with open(dest_fp, 'wb') as f:
                pickle.dump(data, f)
        else:
            return data

    def clear_cache(self) -> None:
        """Clear the request cache and create a new handler."""
        del self._handler
        self._handler = self._get_request_handler(
            cache_limit=self.cache_limit,
            delay=HTTP_DELAY,
            retries=HTTP_RETRY,
            backoff_factor=HTTP_BACKOFF_FACTOR,
            headers=self.headers,
            debug=HTTP_DEBUG,
            verify_https=VERIFY_HTTPS,
        )

    def set_cache_limit(self, new_limit: int) -> None:
        """Set the cache limit for the API's request cache."""
        self._handler.set_cache_limit(cache_limit=new_limit)

    def get_cache_limit(self) -> int:
        """Get the cache limit for the API's request cache."""
        return self._handler.get_cache_limit()

    def get_headers(self) -> dict:
        """Return the current headers used by the request handler."""
        return self._handler.get_headers()

    def update_headers(self, new: dict) -> None:
        """Add new headers to the request handler."""
        self._handler.update_headers(new)

    def set_headers(self, request_headers: dict) -> None:
        """Reset the request headers using the new supplied headers."""
        self._handler.set_headers(request_headers)

    def stations(self, as_df: bool = True) -> Union[pd.DataFrame, dict]:
        """Get all stations and station metadata from the NDBC.

        Query the NDBC data service for the current available data buoys
        (stations), both those maintained by the NDBC and those whose
        measurements are managed by the NDBC. Stations are returned by default
        as rows of a `pandas.DataFrame`, alongside their realtime data coverage
        for some common measurements, their latitude and longitude, and current
        station status notes maintained by the NDBC.

        Args:
            as_df: Flag indicating whether to return current station data as a
                `pandas.DataFrame` if set to `True` or as a `dict` if `False`.

        Returns:
            The current station data from the NDBC data service, either as a
            `pandas.DataFrame` or as a `dict` depending on the value of `as_df`.

        Raises:
            ResponseException: An error occurred while retrieving and parsing
                responses from the NDBC data service.
        """
        try:
            data = self._stations_api.stations(handler=self._handler)
            return self._handle_data(data, as_df, cols=None)
        except (ResponseException, ValueError, KeyError) as e:
            raise ResponseException('Failed to handle returned data.') from e

    def nearest_station(
        self,
        lat: Union[str, float, None] = None,
        lon: Union[str, float, None] = None,
    ) -> str:
        """Get nearest station to the specified lat/lon.

        Use the NDBC data service's current station data to determine the
        nearest station to the specified latitude and longitude (either as
        `float` or as DD.dd[E/W] strings).

        Args:
            lat: The latitude of interest, used to determine the closest
                maintained station to the given position.
            lon: The longitude of interest, used to determine the closest
                maintained station to the given position.

        Returns:
            The station id (e.g. `'tplm2'` or `'41001'`) of the nearest station
            with active measurements to the specified lat/lon pair.

        Raises:
            ValueError: The latitude and longitude were not both specified when
                querying for the closest station.
        """
        if not (lat and lon):
            raise ValueError('lat and lon must be specified.')
        nearest_station = self._stations_api.nearest_station(
            handler=self._handler, lat=lat, lon=lon)
        return nearest_station
    
    def radial_search(
        self,
        lat: Union[str, float, None] = None,
        lon: Union[str, float, None] = None,
        radius: float = -1,
        units: str = 'km',
    ) -> pd.DataFrame:
        """Get all stations within radius units of the specified lat/lon.

        Use the NDBC data service's current station data to determine the
        stations within radius of the specified latitude and longitude
        (passed either as `float` or as DD.dd[E/W] strings).

        Args:
            lat: The latitude of interest, used to determine the maintained
                stations within radius units of the given position.
            lon: The longitude of interest, used to determine the maintained
                stations within radius units of the given position.
            radius: The radius in the specified units to search for stations
                within.
            units: The units of the radius, either 'nm', 'km', or 'mi'.

        Returns:
            A `pandas.DataFrame` of the stations within the specified radius of
            the given lat/lon pair.

        Raises:
            ValueError: The latitude and longitude were not both specified when
                querying for the closest station, or the radius or units are
                invalid.
        """
        if not (lat and lon):
            raise ValueError('lat and lon must be specified.')
        stations_in_radius = self._stations_api.radial_search(
            handler=self._handler, lat=lat, lon=lon, radius=radius, units=units)
        return stations_in_radius

    def station(self,
                station_id: Union[str, int],
                as_df: bool = False) -> Union[pd.DataFrame, dict]:
        """Get metadata for the given station from the NDBC.

        The NDBC maintains some station-level metadata including status notes,
        location information, inclement weather warnings, and measurement notes.
        This method is used to request, handle, and parse the metadata for the
        given station from the station's NDBC webpage.

        Args:
            station_id: The NDBC station ID (e.g. `'tplm2'` or `41001`) for the
                station of interest.
            as_df: Whether to return station-level data as a `pandas.DataFrame`,
                defaults to `False`, and a `dict` is returned.

        Returns:
            The station metadata for the given station, either as a `dict` or as
            a `pandas.DataFrame` if the `as_df` flag is set to `True`.

        Raises:
            ResponseException: An error occurred when requesting and parsing
                responses for the specified station.
        """
        station_id = self._parse_station_id(station_id)
        try:
            data = self._stations_api.metadata(handler=self._handler,
                                               station_id=station_id)
            return self._handle_data(data, as_df, cols=None)
        except (ResponseException, ValueError, KeyError) as e:
            raise ResponseException('Failed to handle returned data.') from e

    def available_realtime(self,
                           station_id: Union[str, int],
                           as_df: bool = False) -> Union[pd.DataFrame, dict]:
        """Get the available realtime measurements for a station.

        While most data buoy (station) measurements are available over
        multi-year time ranges, some measurements depreciate or become
        unavailable for substantial periods of time. This method queries the
        NDBC station webpage for those measurements, and their links, which are
        available or were available over the last 45 days.

        Args:
            station_id: The NDBC station ID (e.g. `'tplm2'` or `41001`) for the
                station of interest.
            as_df: Whether to return station-level data as a `pandas.DataFrame`,
                defaults to `False`, and a `dict` is returned.

        Returns:
            The available realtime measurements for the specified station,
            alongside their NDBC data links, either as a `dict` or as a
            `pandas.DataFrame` if the `as_df` flag is set to `True`.

        Raises:
            ResponseException: An error occurred when requesting and parsing
                responses for the specified station.
        """
        station_id = self._parse_station_id(station_id)
        try:
            data = self._stations_api.realtime(handler=self._handler,
                                               station_id=station_id)
            return self._handle_data(data, as_df, cols=None)
        except (ResponseException, ValueError, KeyError) as e:
            raise ResponseException('Failed to handle returned data.') from e

    def available_historical(self,
                             station_id: Union[str, int],
                             as_df: bool = False) -> Union[pd.DataFrame, dict]:
        """Get the available historical measurements for a station.

        This method queries the NDBC station webpage for historical, quality
        controlled measurements and their associated availability time ranges.

        Args:
            station_id: The NDBC station ID (e.g. `'tplm2'` or `41001`) for the
                station of interest.
            as_df: Whether to return station-level data as a `pandas.DataFrame`,
                defaults to `False`, and a `dict` is returned.

        Returns:
            The available historical measurements for the specified station,
            alongside their NDBC data links, either as a `dict` or as a
            `pandas.DataFrame` if the `as_df` flag is set to `True`.

        Raises:
            ResponseException: An error occurred when requesting and parsing
                responses for the specified station.
        """
        station_id = self._parse_station_id(station_id)
        try:
            data = self._stations_api.historical(handler=self._handler,
                                                 station_id=station_id)
            return self._handle_data(data, as_df, cols=None)
        except (ResponseException, ValueError, KeyError) as e:
            raise ResponseException('Failed to handle returned data.') from e

    def get_data(
        self,
        station_id: Union[int, str, None] = None,
        mode: Union[str, None] = None,
        start_time: Union[str, datetime] = datetime.now() - timedelta(days=30),
        end_time: Union[str, datetime] = datetime.now(),
        use_timestamp: bool = True,
        as_df: bool = True,
        cols: List[str] = None,
        station_ids: Union[List[Union[int, str]], None] = None,
        modes: Union[List[str], None] = None,
    ) -> Union[pd.DataFrame, dict]:
        """Execute data query against the specified NDBC station(s).

        Query the NDBC data service for station-level measurements, using the
        `mode` parameter to determine the measurement type (e.g. `'stdmet'` for
        standard meterological data or `'cwind'` for continuous winds data). The
        time range and data columns of interest may also be specified, such that
        a tailored set of requests are executed against the NDBC data service to
        generate a single `pandas.DataFrame` or `dict` matching the conditions
        specified in the method call. When calling `get_data` with `station_ids`
        the station identifier is added as a column to the returned data.

        Args:
            station_id: The NDBC station ID (e.g. `'tplm2'` or `41001`) for the
                station of interest.
            station_ids: A list of NDBC station IDs (e.g. `['tplm2', '41001']`)
                for the stations of interest.
            mode: The data measurement type to query for the station (e.g.
                `'stdmet'` for standard meterological data or `'cwind'` for
                continuous winds data).
            modes: A list of data measurement types to query for the stations
                (e.g. `['stdmet', 'cwind']`).
            start_time: The first timestamp of interest  (in UTC) for the data
                query, defaulting to 30 days before the current system time.
            end_time: The last timestamp of interest (in UTC) for the data
                query, defaulting to the current system time.
            use_timestamp: A flag indicating whether to parse the NDBC data
                service column headers as a timestamp, and to use this timestamp
                as the index.
            as_df: Whether to return station-level data as a `pandas.DataFrame`,
                defaults to `False`, and a `dict` is returned.
            cols: A list of columns of interest which are selected from the 
                available data columns, such that only the desired columns are
                returned. All columns are returned if `None` is specified.

        Returns:
            The available station(s) measurements for the specified modes, time
            range, and columns, either as a `dict` or as a `pandas.DataFrame`
            if the `as_df` flag is set to `True`.

        Raises:
            ValueError: Both `station_id` and `station_ids` are `None`, or both
                are not `None`. This is also raised if `mode` and `modes` are
                `None`, or both are not `None`
            RequestException: The specified mode is not available.
            ResponseException: There was an error in executing and parsing the
                required requests against the NDBC data service.
            HandlerException: There was an error in handling the returned data
                as a `dict` or `pandas.DataFrame`.
        """
        if station_id is None and station_ids is None:
            raise ValueError('Both `station_id` and `station_ids` are `None`.')
        if station_id is not None and station_ids is not None:
            raise ValueError('`station_id` and `station_ids` cannot both be '
                             'specified.')
        if mode is None and modes is None:
            raise ValueError('Both `mode` and `modes` are `None`.')
        if mode is not None and modes is not None:
            raise ValueError('`mode` and `modes` cannot both be specified.')
        
        handle_station_ids: List[Union[int, str]] = []
        handle_modes: List[str] = []
        station_id_as_column: bool = True if station_id is None else False

        if station_id is not None:
            handle_station_ids.append(station_id)
        if station_ids is not None:
            handle_station_ids.extend(station_ids)
        if mode is not None:
            handle_modes.append(mode)
        if modes is not None:
            handle_modes.extend(modes)
        
        # accumulated_data records the handled response and parsed station_id
        # as a tuple, with the data as the first value and the id as the second.
        accumulated_data: List[Tuple[Union[pd.DataFrame, dict], str]] = []
        with ThreadPoolExecutor(max_workers=len(handle_station_ids) * len(handle_modes)) as executor:
            futures = [
                executor.submit(
                    self._handle_get_data,
                    station_id=station_id,
                    mode=mode,
                    start_time=start_time,
                    end_time=end_time,
                    use_timestamp=use_timestamp,
                    as_df=as_df,
                    cols=cols
                )
                for station_id, mode in itertools.product(handle_station_ids, handle_modes)
            ]
            for future in futures:
                try:
                    data = future.result()
                    accumulated_data.append(data)
                except (RequestException, ResponseException, HandlerException) as e:
                    self.log.error(
                        f"Failed to process request for station_id {station_id} "
                        f"and mode {mode} with error: {e}"
                    )
                    continue

        # check that we have some response
        if len(accumulated_data) == 0:
            raise ResponseException(f'No data was returned for station_ids {handle_station_ids} '
                                    f'and modes {handle_modes}')
        # handle the default case where a single station_id and mode are specified
        if len(accumulated_data) == 1:
            return accumulated_data[0][0]
        # handle the case where multiple station_ids and modes are specified
        return self._handle_accumulate_data(accumulated_data, station_id_as_column)

    def get_modes(self):
        """Get the list of supported modes for `get_data(...)`."""
        return [v for v in vars(self._data_api) if not v.startswith('_')]

    """ PRIVATE """

    def _get_request_handler(
        self,
        cache_limit: int,
        delay: int,
        retries: int,
        backoff_factor: float,
        headers: dict,
        debug: bool,
        verify_https: bool,
    ) -> Any:
        """Build a new `RequestHandler` for the `NdbcApi`."""
        return RequestHandler(
            cache_limit=cache_limit or self.cache_limit,
            log=self.log,
            delay=delay,
            retries=retries,
            backoff_factor=backoff_factor,
            headers=headers,
            debug=debug,
            verify_https=verify_https,
        )

    @staticmethod
    def _parse_station_id(station_id: Union[str, int]) -> str:
        """Parse station id."""
        station_id = str(station_id)  # expect string-valued station id
        station_id = station_id.lower()  # expect lowercased station id
        return station_id

    @staticmethod
    def _handle_timestamp(timestamp: Union[datetime, str]) -> datetime:
        """Convert the specified timestamp to `datetime.datetime`."""
        if isinstance(timestamp, datetime):
            return timestamp
        else:
            try:
                return datetime.strptime(timestamp, '%Y-%m-%d')
            except ValueError as e:
                raise TimestampException from e

    @staticmethod
    def _enforce_timerange(df: pd.DataFrame, start_time: datetime,
                           end_time: datetime) -> pd.DataFrame:
        """Down-select to the data within the specified `datetime` range."""
        try:
            df = df.loc[(df.index.values >= pd.Timestamp(start_time)) &
                        (df.index.values <= pd.Timestamp(end_time))]
        except ValueError as e:
            raise TimestampException(
                'Failed to enforce `start_time` to `end_time` range.') from e
        return df

    @staticmethod
    def _handle_data(data: pd.DataFrame,
                     as_df: bool = True,
                     cols: List[str] = None) -> Union[pd.DataFrame, dict]:
        """Apply column down selection and return format handling."""
        if cols:
            try:
                data = data[[*cols]]
            except (KeyError, ValueError) as e:
                raise ParserException(
                    'Failed to parse column selection.') from e
        if as_df and isinstance(data, pd.DataFrame):
            return data
        elif isinstance(data, pd.DataFrame) and not as_df:
            return data.to_dict()
        elif as_df:
            try:
                return pd.DataFrame().from_dict(data, orient='index')
            except (NotImplementedError, ValueError, TypeError) as e:
                raise HandlerException(
                    'Failed to convert `pd.DataFrame` to `dict`.') from e
        else:
            return data
    
    @staticmethod
    def _handle_accumulate_data(
        accumulated_data: List[Tuple[Union[pd.DataFrame, dict], str]],
        station_id_as_column: bool = False
    ) -> Union[pd.DataFrame, dict]:
        return_as_df = isinstance(accumulated_data[0][0], pd.DataFrame)
        data: Union[List[pd.DataFrame], dict] = [] if return_as_df else {}
   
        for d in accumulated_data:
            if return_as_df:
                if station_id_as_column:
                    d[0].insert(0, 'station_id', d[1])
                data.append(d[0])
            else:
                d_data, d_station_id = d[0], d[1]
                if station_id_as_column:
                    # the keys need to be updated to include the station_id
                    # as a prefix before we update the `data` dict.
                    d_data = {f'{d_station_id}_{k}': v for k, v in d_data.items()}
                # the keys across modes should be unique so a simple update
                # is sufficient.
                data.update(d_data)

        if return_as_df:
            return pd.concat(data)
        return data

    def _handle_get_data(
        self,
        mode: str,
        station_id: str,
        start_time: datetime,
        end_time: datetime,
        use_timestamp: bool,
        as_df: bool = True,
        cols: List[str] = None
    ) -> Tuple[Union[pd.DataFrame, dict], str]:
        start_time = self._handle_timestamp(start_time)
        end_time = self._handle_timestamp(end_time)
        station_id = self._parse_station_id(station_id)
        data_api_call = getattr(self._data_api, mode, None)
        if not data_api_call:
            raise RequestException(
                'Please supply a supported mode from `get_modes()`.')
        try:
            data = data_api_call(
                self._handler,
                station_id,
                start_time,
                end_time,
                use_timestamp,
            )
        except (ResponseException, ValueError, TypeError, KeyError) as e:
            raise ResponseException(f'Failed to handle API call.\nRaised from {e}') from e
        if use_timestamp:
            data = self._enforce_timerange(df=data,
                                           start_time=start_time,
                                           end_time=end_time)
        try:
            handled_data = self._handle_data(data, as_df, cols)
        except (ValueError, KeyError, AttributeError) as e:
            raise ParserException(f'Failed to handle returned data.\nRaised from {e}') from e

        return (handled_data, station_id)
