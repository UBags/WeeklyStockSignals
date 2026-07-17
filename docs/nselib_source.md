# nselib source (v2.5.1)
Full Python source of the `nselib` package, bundled from a clean PyPI install of nselib==2.5.1 (matches the `corporate_actions_for_equity` / `NSEdataNotFound` call sites in your traceback). One section per module, in the same `#### path` + fenced-code format as the project bundle.

Bundled: 2026-07-17. 21 modules.

## File index

- `nselib/__init__.py`
- `nselib/capital_market/__init__.py`
- `nselib/capital_market/capital_market_data.py`
- `nselib/capital_market/get_func.py`
- `nselib/cash_market/__init__.py`
- `nselib/cash_market/cash_market_data.py`
- `nselib/constants.py`
- `nselib/debt/__init__.py`
- `nselib/debt/debt_data.py`
- `nselib/derivatives/__init__.py`
- `nselib/derivatives/derivative_data.py`
- `nselib/derivatives/get_func.py`
- `nselib/errors.py`
- `nselib/indices/__init__.py`
- `nselib/indices/index_data.py`
- `nselib/indices/nse_config.py`
- `nselib/libutil.py`
- `nselib/logger.py`
- `nselib/mutual_funds/__init__.py`
- `nselib/mutual_funds/mutual_fund_data.py`
- `nselib/nsdl_fpi.py`

---

#### `nselib/__init__.py`

```python
import logging

from .libutil import trading_holiday_calendar
from .logger import enable_logging

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "2.5.1"
__all__ = ["trading_holiday_calendar", "enable_logging"]

```

#### `nselib/capital_market/__init__.py`

```python
from .capital_market_data import price_volume_and_deliverable_position_data, price_volume_data, \
    deliverable_position_data, bulk_deal_data, block_deals_data, short_selling_data, \
    bhav_copy_with_delivery, bhav_copy_equities, equity_list, fno_equity_list, fno_index_list, nifty50_equity_list, \
    niftynext50_equity_list, niftymidcap150_equity_list, niftysmallcap250_equity_list, india_vix_data, \
    index_data, market_watch_all_indices, daily_volatility, var_begin_day, var_1st_intra_day, var_2nd_intra_day, var_3rd_intra_day, \
    var_4th_intra_day, var_end_of_day, sme_bhav_copy, sme_band_complete, week_52_high_low_report, financial_results_for_equity, \
    corporate_bond_trade_report, bhav_copy_sme, pe_ratio, corporate_actions_for_equity, event_calendar_for_equity, \
    top_gainers_or_losers, most_active_equities, total_traded_stocks, category_turnover_cash, business_growth_cm_segment

```

#### `nselib/capital_market/capital_market_data.py`

```python
import datetime as dt
import zipfile
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime
import logging
import requests
from io import BytesIO

from nselib.capital_market.get_func import *
from nselib.libutil import *
from nselib.constants import (
    dd_mm_yyyy,
    ddmmyyyy,
    ddmmyy,
    price_volume_and_deliverable_position_data_columns,
    price_volume_data_columns,
    deliverable_data_columns,
    india_vix_data_column,
    bulk_deal_data_columns,
    block_deals_data_columns,
    short_selling_data_columns,
    var_columns,
)
from nselib.errors import NSEdataNotFound

logger = logging.getLogger(__name__)


def price_volume_and_deliverable_position_data(symbol: str, from_date: str = None, to_date: str = None,
                                               period: str = None):
    """
    get Security wise price volume & Deliverable position data set. use get_nse_symbols() to get all symbols
    :param symbol: symbol eg: 'SBIN'
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,'1W': for last 7 days data,
                            '1M': from last month same date, '6M': last 6 month data, '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
    """
    validate_date_param(from_date, to_date, period)
    symbol = cleaning_nse_symbol(symbol=symbol)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=price_volume_and_deliverable_position_data_columns)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_price_volume_and_deliverable_position_data(symbol=symbol, from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if not data_df.empty:
            # Drop all-NaN columns from both frames
            data_df = data_df.fillna('-')
            nse_df = nse_df.fillna('-')
            data_df = data_df.dropna(axis=1, how='all')
            nse_df = nse_df.dropna(axis=1, how='all')
            if not data_df.empty:
                nse_df = pd.concat([nse_df, data_df], ignore_index=True)

    nse_df["TotalTradedQuantity"] = pd.to_numeric(nse_df["TotalTradedQuantity"].astype(str).str.replace(",", ""), errors="coerce")
    nse_df["TurnoverInRs"] = pd.to_numeric(nse_df["TurnoverInRs"].astype(str).str.replace(",", ""), errors="coerce")
    nse_df["No.ofTrades"] = pd.to_numeric(nse_df["No.ofTrades"].astype(str).str.replace(",", ""), errors="coerce")
    nse_df["DeliverableQty"] = pd.to_numeric(nse_df["DeliverableQty"].astype(str).str.replace(",", ""), errors="coerce")
    return nse_df


def price_volume_data(symbol: str, from_date: str = None, to_date: str = None, period: str = None):
    """
    get Security wise price volume data set.
    :param symbol: symbol eg: 'SBIN'
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,'1W': for last 7 days data,
                            '1M': from last month same date, '6M': last 6 month data, '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.price_volume_data('SBIN', '17-03-2022', '17-06-2023', period='1M')
    """
    logger.debug(f"Fetching data for price_volume_data")
    validate_date_param(from_date, to_date, period)
    symbol = cleaning_nse_symbol(symbol=symbol)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=price_volume_data_columns)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_price_volume_data(symbol=symbol, from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def deliverable_position_data(symbol: str, from_date: str = None, to_date: str = None, period: str = None):
    """
    get Security wise deliverable position data set.
    :param symbol: symbol eg: 'SBIN'
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.deliverable_position_data('SBIN', '17-03-2022', '17-06-2023', period='1M')
    """
    logger.debug(f"Fetching data for deliverable_position_data")
    validate_date_param(from_date, to_date, period)
    symbol = cleaning_nse_symbol(symbol=symbol)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=deliverable_data_columns)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_deliverable_position_data(symbol=symbol, from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def india_vix_data(from_date: str = None, to_date: str = None, period: str = None):
    """
    get india vix spot data  set for the specific time period.
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,'1W': for last 7 days data,
                            '1M': from last month same date, '6M': last 6 month data, '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.india_vix_data('17-03-2022', '17-06-2023', period='1M')
    """
    logger.debug(f"Fetching data for india_vix_data")
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=india_vix_data_column)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_india_vix_data(from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def index_data(index: str, from_date: str = None, to_date: str = None, period: str = None):
    """
    get historical index data set for the specific time period.
    apply the index name as per the nse india site
    :param index: 'NIFTY 50'/'NIFTY BANK'
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,'1W': for last 7 days data,
                            '1M': from last month same date, '6M': last 6 month data, '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
         from nselib import capital_market
         df = capital_market.index_data('17-03-2022', '17-06-2023', period='1M', 'NIFTY 50')
    """
    logger.debug(f"Fetching data for index_data")
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame()
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_index_data(index=index, from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def bulk_deal_data(from_date: str = None, to_date: str = None, period: str = None):
    """
    get bulk deal data set.
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.bulk_deal_data('17-03-2022', '17-06-2023', period='1M')
    """
    logger.debug(f"Fetching data for bulk_deal_data")
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=bulk_deal_data_columns)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_bulk_deal_data(from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def block_deals_data(from_date: str = None, to_date: str = None, period: str = None):
    """
    get block deals data set.
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.block_deals_data('17-03-2022', '17-06-2023', period='1M')
    """
    logger.debug(f"Fetching data for block_deals_data")
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=block_deals_data_columns)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_block_deals_data(from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def short_selling_data(from_date: str = None, to_date: str = None, period: str = None):
    """
    get short selling data set.
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.short_selling_data('17-03-2022', '17-06-2023', period='1M')
    """
    logger.debug(f"Fetching data for short_selling_data")
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    nse_df = pd.DataFrame(columns=short_selling_data_columns)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 365:
            end_date = (from_date + dt.timedelta(364)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_short_selling_data(from_date=start_date, to_date=end_date)
        from_date = from_date + dt.timedelta(365)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    return nse_df


def bhav_copy_with_delivery(trade_date: str):
    """
    get the NSE bhav copy with delivery data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.bhav_copy_with_delivery('17-03-2022')
    """
    logger.debug(f"Fetching data for bhav_copy_with_delivery")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{use_date}.csv'
    request_bhav = nse_urlfetch(url)
    if request_bhav.status_code == 200:
        bhav_df = pd.read_csv(BytesIO(request_bhav.content))
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    bhav_df.columns = [name.replace(' ', '') for name in bhav_df.columns]
    bhav_df['SERIES'] = bhav_df['SERIES'].str.replace(' ', '')
    bhav_df['DATE1'] = bhav_df['DATE1'].str.replace(' ', '')
    return bhav_df


def bhav_copy_equities(trade_date: str):
    """
    get new CM-UDiFF Common Bhavcopy Final as per the traded date provided
    :param trade_date:
    :return: pandas dataframe
        Example:
            from nselib import capital_market
            df = capital_market.bhav_copy_equities('17-03-2022')
    """
    logger.debug(f"Fetching data for bhav_copy_equities")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    url = 'https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_'
    payload = f"{str(trade_date.strftime('%Y%m%d'))}_F_0000.csv.zip"
    request_bhav = nse_urlfetch(url + payload)
    bhav_df = pd.DataFrame()
    if request_bhav.status_code == 200:
        zip_bhav = zipfile.ZipFile(BytesIO(request_bhav.content), 'r')
        for file_name in zip_bhav.filelist:
            if file_name:
                bhav_df = pd.read_csv(zip_bhav.open(file_name))
    elif request_bhav.status_code == 403:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    # bhav_df = bhav_df[['SYMBOL', 'SERIES', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'LAST', 'PREVCLOSE', 'TOTTRDQTY',
    #                    'TOTTRDVAL', 'TIMESTAMP', 'TOTALTRADES']]
    return bhav_df


def bhav_copy_indices(trade_date: str):
    """
    get nse bhav copy as per the traded date provided
    :param trade_date: eg:'20-06-2023'
    :return: pandas dataframe
        Example:
            from nselib import capital_market
            df = capital_market.bhav_copy_indices('17-03-2022')
    """
    logger.debug(f"Fetching data for bhav_copy_indices")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    url = f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{str(trade_date.strftime('%d%m%Y').upper())}.csv"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        raise FileNotFoundError(f" No data available for : {trade_date}")
    try:
        bhav_df = pd.read_csv(BytesIO(file_chk.content))
    except Exception as e:
        raise FileNotFoundError(f' Bhav copy indices not found for : {trade_date} :: NSE error : {e}')
    return bhav_df


def bhav_copy_sme(trade_date: str):
    """
    get the NSE bhav copy for SME data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.bhav_copy_sme('17-03-2022')
    """
    logger.debug(f"Fetching data for bhav_copy_sme")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyy)
    url = f'https://nsearchives.nseindia.com/archives/sme/bhavcopy/sme{use_date}.csv'
    request_bhav = nse_urlfetch(url)
    if request_bhav.status_code == 200:
        bhav_df = pd.read_csv(BytesIO(request_bhav.content))
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    bhav_df.columns = [name.replace(' ', '') for name in bhav_df.columns]
    return bhav_df


def equity_list():
    """
    get list of all equity available to trade in NSE
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.equity_list()
    """
    logger.debug(f"Fetching data for equity_list")
    origin_url = "https://nsewebsite-staging.nseindia.com"
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    file_chk = nse_urlfetch(url, origin_url=origin_url)
    if file_chk.status_code != 200:
        raise FileNotFoundError(f" No data equity list available")
    try:
        data_df = pd.read_csv(BytesIO(file_chk.content))
    except Exception as e:
        raise FileNotFoundError(f' Equity List not found :: NSE error : {e}')
    data_df = data_df[['SYMBOL', 'NAME OF COMPANY', ' SERIES', ' DATE OF LISTING', ' FACE VALUE']]
    return data_df


def fno_equity_list():
    """
    get a dataframe of all listed derivative equity list with the recent lot size to trade
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.fno_equity_list()
    """
    logger.debug(f"Fetching data for fno_equity_list")
    origin_url = "https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-information"
    url = "https://www.nseindia.com/api/underlying-information"
    data_obj = nse_urlfetch(url, origin_url=origin_url)
    if data_obj.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for fno_equity_list")
    data_dict = data_obj.json()
    data_df = pd.DataFrame(data_dict['data']['UnderlyingList'])
    return data_df


def fno_index_list():
    """
    get a dataframe of all listed derivative index list with the recent lot size to trade
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.fno_index_list()
    """
    logger.debug(f"Fetching data for fno_index_list")
    origin_url = "https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-information"
    url = "https://www.nseindia.com/api/underlying-information"
    data_obj = nse_urlfetch(url, origin_url=origin_url)
    if data_obj.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for fno_equity_index_list")
    data_dict = data_obj.json()
    data_df = pd.DataFrame(data_dict['data']['IndexList'])
    return data_df


def nifty50_equity_list():
    """
    list of all equities under NIFTY 50 index
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.nifty50_equity_list()
    """
    logger.debug(f"Fetching data for nifty50_equity_list")
    url = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        raise FileNotFoundError(f" No data equity list available")
    try:
        data_df = pd.read_csv(BytesIO(file_chk.content))
    except Exception as e:
        raise FileNotFoundError(f' equities under NIFTY 50 index not found :: NSE error : {e}')
    data_df = data_df[['Company Name', 'Industry', 'Symbol']]
    return data_df


def niftynext50_equity_list():
    """
    list of all equities under NIFTY NEXT 50 index
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.niftynext50_equity_list()
    """
    logger.debug(f"Fetching data for niftynext50_equity_list")
    try:
        data_df = pd.read_csv("https://archives.nseindia.com/content/indices/ind_niftynext50list.csv")
    except Exception as e:
        raise FileNotFoundError(f' equities under NIFTY NEXT 50 index not found :: NSE error : {e}')
    data_df = data_df[['Company Name', 'Industry', 'Symbol']]
    return data_df


def niftymidcap150_equity_list():
    """
    list of all equities under NIFTY MIDCAP 150 index
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.niftymidcap150_equity_list()
    """
    logger.debug(f"Fetching data for niftymidcap150_equity_list")
    try:
        data_df = pd.read_csv("https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv")
    except Exception as e:
        raise FileNotFoundError(f' equities under NIFTY MIDCAP 150 index not found :: NSE error : {e}')
    data_df = data_df[['Company Name', 'Industry', 'Symbol']]
    return data_df


def niftysmallcap250_equity_list():
    """
    list of all equities under NIFTY SMALLCAP 250 index
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.niftysmallcap250_equity_list()
    """
    logger.debug(f"Fetching data for niftysmallcap250_equity_list")
    try:
        data_df = pd.read_csv("https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv")
    except Exception as e:
        raise FileNotFoundError(f' equities under NIFTY SMALLCAP 250 index not found :: NSE error : {e}')
    data_df = data_df[['Company Name', 'Industry', 'Symbol']]
    return data_df


def market_watch_all_indices():
    """
    Market Watch - Indices of the day in data frame
    :return: pd.DataFrame
        Example:
            from nselib import capital_market
            df = capital_market.market_watch_all_indices()
    """
    logger.debug(f"Fetching data for market_watch_all_indices")
    origin_url = "https://nsewebsite-staging.nseindia.com"
    url = "https://www.nseindia.com/api/allIndices"
    data_json = nse_urlfetch(url, origin_url=origin_url).json()
    data_df = pd.DataFrame(data_json['data'])
    print(data_df.columns)
    return data_df[['key', 'index', 'indexSymbol', 'last', 'variation', 'percentChange', 'open', 'high', 'low',
                    'previousClose', 'yearHigh', 'yearLow', 'pe', 'pb', 'dy', 'declines', 'advances', 'unchanged',
                    'perChange365d', 'perChange30d', 'previousDay', 'oneWeekAgoVal', 'oneMonthAgoVal', 'oneYearAgoVal']]


def fii_dii_trading_activity():
    """
    FII and DII trading activity of the day in data frame
    :return: pd.DataFrame
        Example:
            from nselib import capital_market
            df = capital_market.fii_dii_trading_activity()
    """
    logger.debug(f"Fetching data for fii_dii_trading_activity")
    url = "https://www.nseindia.com/api/fiidiiTradeReact"
    data_json = nse_urlfetch(url).json()
    data_df = pd.DataFrame(data_json)
    return data_df


def daily_volatility(trade_date: str):
    """
    get CM daily volatility report as per the traded date provided
    :param trade_date: eg:'20-06-2023'
    :return: pandas dataframe
        Example:
            from nselib import capital_market
            df = capital_market.daily_volatility('17-03-2022')
    """
    logger.debug(f"Fetching data for daily_volatility")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    payload = f"CMVOLT_{str(trade_date.strftime('%d%m%Y'))}.CSV"
    origin_url = "https://www.nseindia.com/all-reports"
    report_urls = [
        f"https://nsearchives.nseindia.com/archives/nsccl/volt/{payload}",
        f"https://archives.nseindia.com/archives/nsccl/volt/{payload}",
        "https://www.nseindia.com/api/reports?archives="
        "%5B%7B%22name%22%3A%22CM%20-%20Daily%20Volatility%22%2C%22type%22%3A%22archives%22%2C%22category%22"
        f"%3A%22capital-market%22%2C%22section%22%3A%22equity%22%7D%5D&date={str(trade_date.strftime('%d-%b-%Y'))}"
        f"&type=equity&mode=single",
    ]
    last_status_code = None
    for url in report_urls:
        # NSE archive requests can fail behind stale local proxy settings, so fetch this report
        # with a session that ignores environment proxies while preserving the usual cookie flow.
        r_session = requests.session()
        r_session.trust_env = False
        nse_live = r_session.get(origin_url, headers=default_header)
        cookies = nse_live.cookies
        report = r_session.get(url, headers=header, cookies=cookies)
        last_status_code = report.status_code
        if report.status_code != 200:
            continue
        try:
            data_df = pd.read_csv(BytesIO(report.content), skipinitialspace=True)
        except Exception as exc:
            raise FileNotFoundError(
                f" CM daily volatility data not found for : {trade_date.strftime(dd_mm_yyyy)} :: NSE error : {exc}"
            )
        data_df = data_df.dropna(how='all')
        data_df.columns = [column_name.strip() for column_name in data_df.columns]
        return data_df
    raise FileNotFoundError(
        f" No CM daily volatility data available for : {trade_date.strftime(dd_mm_yyyy)} :: status={last_status_code}"
    )


def category_turnover_cash(trade_date: str):
    """
    get NSE cash market category-wise turnover data as per the traded date provided
    :param trade_date: eg:'07-04-2026'
    :return: pandas dataframe
        Example:
            from nselib import capital_market
            df = capital_market.category_turnover_cash('17-03-2022')
    """
    logger.debug(f"Fetching data for category_turnover_cash")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    url = f"https://archives.nseindia.com/archives/equities/cat/cat_turnover_{trade_date.strftime(ddmmyy)}.xls"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        raise FileNotFoundError(f" No data available for : {trade_date}")

    raw = pd.read_excel(BytesIO(file_chk.content), header=None, engine="xlrd")
    blocks = []
    row_count = len(raw.index)
    row_number = 0
    while row_number < row_count:
        first = str(raw.iloc[row_number, 0]).strip() if raw.shape[1] > 0 else ""
        second = str(raw.iloc[row_number, 1]).strip() if raw.shape[1] > 1 else ""
        if first.lower() == "trade date" and second.lower() in {"category", "client categories"}:
            headers = [str(value).strip() for value in raw.iloc[row_number, :4].tolist()]
            start_row = row_number + 1
            end_row = start_row
            while end_row < row_count:
                first_value = raw.iloc[end_row, 0] if raw.shape[1] > 0 else None
                second_value = raw.iloc[end_row, 1] if raw.shape[1] > 1 else None
                first_text = str(first_value).strip() if first_value is not None else ""
                second_text = str(second_value).strip() if second_value is not None else ""
                if (
                    first_value is None
                    or (isinstance(first_value, float) and pd.isna(first_value))
                    or first_text.lower().startswith("note")
                    or first_text.lower().startswith("notes")
                    or first_text.lower() == "trade date"
                    or second_text.lower() in {"category", "client categories"}
                ):
                    break
                end_row += 1
            block = raw.iloc[start_row:end_row, :4].copy()
            block.columns = headers
            blocks.append(block)
            row_number = end_row
            continue
        row_number += 1

    if not blocks:
        raise FileNotFoundError(f" Category turnover cash data not found for : {trade_date}")

    data_df = pd.concat(blocks, ignore_index=True)
    data_df = data_df.rename(
        columns={
            "Trade Date": "Trade Date",
            "Category": "Category",
            "Client Categories": "Category",
            "Buy Value in Rs.": "Buy Value in Rs.Crores",
            "Buy Value in Rs.Crores": "Buy Value in Rs.Crores",
            "Sell Value in Rs.": "Sell Value in Rs.Crores",
            "Sell Value in Rs.Crores": "Sell Value in Rs.Crores",
        }
    )
    data_df = data_df[
        [
            column
            for column in [
                "Trade Date",
                "Category",
                "Buy Value in Rs.Crores",
                "Sell Value in Rs.Crores",
            ]
            if column in data_df.columns
        ]
    ].copy()
    data_df["Trade Date"] = pd.to_datetime(data_df["Trade Date"], errors="coerce")
    data_df["Category"] = data_df["Category"].astype(str).str.strip()
    data_df = data_df[data_df["Category"].ne("")].copy()
    for column_name in ["Buy Value in Rs.Crores", "Sell Value in Rs.Crores"]:
        data_df[column_name] = pd.to_numeric(data_df[column_name], errors="coerce")
    data_df = data_df.dropna(subset=["Trade Date", "Category"]).reset_index(drop=True)
    data_df["Net Value in Rs.Crores"] = (
        data_df["Buy Value in Rs.Crores"] - data_df["Sell Value in Rs.Crores"]
    )
    data_df["Trade Date"] = data_df["Trade Date"].dt.strftime("%d-%b-%Y")
    return data_df


def var_begin_day(trade_date: str):
    """
    get the VaR Begin Day data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.var_begin_day('17-03-2022')
    """
    logger.debug(f"Fetching data for var_begin_day")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/archives/nsccl/var/C_VAR1_{use_date}_1.DAT'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        var_df = pd.read_csv(BytesIO(request_nse.content), skiprows=1)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    var_df.columns = var_columns
    return var_df


def var_1st_intra_day(trade_date: str):
    """
    get the VaR 1st Intra Day data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.var_1st_intra_day('17-03-2022')
    """
    logger.debug(f"Fetching data for var_1st_intra_day")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/archives/nsccl/var/C_VAR1_{use_date}_2.DAT'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        var_df = pd.read_csv(BytesIO(request_nse.content), skiprows=1)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    var_df.columns = var_columns
    return var_df


def var_2nd_intra_day(trade_date: str):
    """
    get the VaR 2nd Intra Day data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.var_2nd_intra_day('17-03-2022')
    """
    logger.debug(f"Fetching data for var_2nd_intra_day")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/archives/nsccl/var/C_VAR1_{use_date}_3.DAT'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        var_df = pd.read_csv(BytesIO(request_nse.content), skiprows=1)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    var_df.columns = var_columns
    return var_df


def var_3rd_intra_day(trade_date: str):
    """
    get the VaR 3rd Intra Day data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.var_3rd_intra_day('17-03-2022')
    """
    logger.debug(f"Fetching data for var_3rd_intra_day")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/archives/nsccl/var/C_VAR1_{use_date}_4.DAT'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        var_df = pd.read_csv(BytesIO(request_nse.content), skiprows=1)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    var_df.columns = var_columns
    return var_df


def var_4th_intra_day(trade_date: str):
    """
    get the VaR 4th Intra Day data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.var_4th_intra_day('17-03-2022')
    """
    logger.debug(f"Fetching data for var_4th_intra_day")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/archives/nsccl/var/C_VAR1_{use_date}_5.DAT'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        var_df = pd.read_csv(BytesIO(request_nse.content), skiprows=1)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    var_df.columns = var_columns
    return var_df


def var_end_of_day(trade_date: str):
    """
    get the VaR End of Day data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.var_end_of_day('17-03-2022')
    """
    logger.debug(f"Fetching data for var_end_of_day")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/archives/nsccl/var/C_VAR1_{use_date}_6.DAT'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        var_df = pd.read_csv(BytesIO(request_nse.content), skiprows=1)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    var_df.columns = var_columns
    return var_df


def sme_bhav_copy(trade_date: str):
    """
    get the SME bhav copy data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.sme_bhav_copy('17-03-2022')
    """
    logger.debug(f"Fetching data for sme_bhav_copy")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyy)
    url = f'https://nsearchives.nseindia.com/archives/sme/bhavcopy/sme{use_date}.csv'
    request_bhav = nse_urlfetch(url)
    if request_bhav.status_code == 200:
        bhav_df = pd.read_csv(BytesIO(request_bhav.content))
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    bhav_df.columns = [name.replace(' ', '') for name in bhav_df.columns]
    return bhav_df


def sme_band_complete(trade_date: str):
    """
    get the SME Band Complete data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.sme_band_complete('17-03-2022')
    """
    logger.debug(f"Fetching data for sme_band_complete")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/sme/content/price_band/archieves/sme_bands_complete_{use_date}.csv'
    request_sme = nse_urlfetch(url)
    if request_sme.status_code == 200:
        sme_df = pd.read_csv(BytesIO(request_sme.content))
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    sme_df.columns = [name.replace(' ', '') for name in sme_df.columns]
    return sme_df


def week_52_high_low_report(trade_date: str):
    """
    get the 52-Week High Low Report data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.week_52_high_low_report('17-03-2022')
    """
    logger.debug(f"Fetching data for week_52_high_low_report")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyyyy)
    url = f'https://nsearchives.nseindia.com/content/CM_52_wk_High_low_{use_date}.csv'
    request_nse = nse_urlfetch(url)
    if request_nse.status_code == 200:
        high_low_df = pd.read_csv(BytesIO(request_nse.content), skiprows=2)
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    high_low_df.columns = [name.replace(' ', '') for name in high_low_df.columns]
    return high_low_df


def financial_results_for_equity(from_date: str = None,
                                 to_date: str = None,
                                 period: str = None,
                                 fo_sec: bool = False,
                                 fin_period: str = 'Quarterly'):

    """
    get audited and un-auditable financial results for equities. as per
    https://www.nseindia.com/companies-listing/corporate-filings-financial-results
    :param fin_period: Quaterly/ Half-Yearly/ Annual/ Others
    :param fo_sec: True/False
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
    """
    master_data_df, headers, ns, keys_to_extract = get_financial_results_master(from_date, to_date, period,
                                                                                fo_sec, fin_period)
    fin_df, df = pd.DataFrame(), pd.DataFrame()
    for row in master_data_df.itertuples(index=False):
        try:
            # Fetch the XML content
            response_ = requests.get(row.xbrl, headers=headers)
            response_.raise_for_status()  # Check for HTTP errors

            # Parse the XML content
            root = ET.fromstring(response_.content)

            # Extract values
            extracted_data = {}
            for key in keys_to_extract:
                elem_ = root.find(f".//in-bse-fin:{key}", ns)
                extracted_data[key] = elem_.text if elem_ is not None else None

            # Convert to Pandas DataFrame
            df = pd.DataFrame([extracted_data])
        except requests.exceptions.RequestException as e:
            raise requests.exceptions.RequestException(f"Request failed: {e}") from e
        except ET.ParseError as e:
            raise ET.ParseError(f"XML parsing failed: {e}") from e
        except Exception as e:
            raise RuntimeError(f"An error occurred: {e}") from e

        if fin_df.empty:
            fin_df = df
        else:
            fin_df = pd.concat([fin_df, df], ignore_index=True)
    return fin_df


def corporate_bond_trade_report(trade_date: str):
    """
    get the NSE corporate bond trade report as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.corporate_bond_trade_report('17-03-2022')
    """
    logger.debug(f"Fetching data for corporate_bond_trade_report")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyy)
    url = f'https://nsearchives.nseindia.com/archives/equities/corpbond/corpbond{use_date}.csv'
    request_bhav = nse_urlfetch(url)
    if request_bhav.status_code == 200:
        bond_df = pd.read_csv(BytesIO(request_bhav.content))
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    bond_df.columns = [name.replace(' ', '') for name in bond_df.columns]
    bond_df['SERIES'] = bond_df['SERIES'].str.replace(' ', '')
    return bond_df


def pe_ratio(trade_date: str):
    """
    get the NSE pe ratio for all NSE equities data as per the traded date
    :param trade_date: eg:'20-06-2023'
    :return: pandas data frame
        Example:
            from nselib import capital_market
            df = capital_market.pe_ratio('17-03-2022')
    """
    logger.debug(f"Fetching data for pe_ratio")
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    use_date = trade_date.strftime(ddmmyy)
    url = f'https://nsearchives.nseindia.com/content/equities/peDetail/PE_{use_date}.csv'
    request_bhav = nse_urlfetch(url)
    if request_bhav.status_code == 200:
        pe_df = pd.read_csv(BytesIO(request_bhav.content))
    else:
        raise FileNotFoundError(f' Data not found, change the trade_date...')
    pe_df.columns = [name.replace(' ', '') for name in pe_df.columns]
    return pe_df


def corporate_actions_for_equity(from_date: str = None,
                                 to_date: str = None,
                                 period: str = None,
                                 fno_only: bool = False):

    """
    get corporate actions for equities as per
    https://www.nseindia.com/companies-listing/corporate-filings-actions
    :param fno_only: True/False
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
    """
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    origin_url = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
    url_ = "https://www.nseindia.com/api/corporates-corporateactions?index=equities&"
    if fno_only:
        payload = f'from_date={from_date}&to_date={to_date}&fo_sec=true'
    else:
        payload = f'from_date={from_date}&to_date={to_date}'
    data_text = nse_urlfetch(url_ + payload, origin_url=origin_url)
    if data_text.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for financial data with these parameters")
    json_str = data_text.content.decode("utf-8")
    data_list = json.loads(json_str)
    master_data_df = pd.DataFrame(data_list)
    master_data_df.columns = [name.replace(' ', '') for name in master_data_df.columns]
    return master_data_df


def event_calendar_for_equity(from_date: str = None,
                              to_date: str = None,
                              period: str = None,
                              fno_only: bool = False):

    """
    get event calendar for equities as per
    https://www.nseindia.com/companies-listing/corporate-filings-event-calendar
    :param fno_only: True/False
    :param from_date: '17-03-2022' ('dd-mm-YYYY')
    :param to_date: '17-06-2023' ('dd-mm-YYYY')
    :param period: use one {'1D': last day data,
                            '1W': for last 7 days data,
                            '1M': from last month same date,
                            '6M': last 6 month data,
                            '1Y': from last year same date)
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
    """
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
    origin_url = "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
    url_ = "https://www.nseindia.com/api/event-calendar?index=equities&"
    if fno_only:
        payload = f'from_date={from_date}&to_date={to_date}&fo_sec=true'
    else:
        payload = f'from_date={from_date}&to_date={to_date}'
    data_text = nse_urlfetch(url_ + payload, origin_url=origin_url)
    if data_text.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for financial data with these parameters")
    json_str = data_text.content.decode("utf-8")
    data_list = json.loads(json_str)
    master_data_df = pd.DataFrame(data_list)
    master_data_df.columns = [name.replace(' ', '') for name in master_data_df.columns]
    return master_data_df


def top_gainers_or_losers(to_get: str = 'gainers'):
    """
    get top gainers or losers on live market, after market hour it will get as per last traded value
    :param to_get: gainers/loosers
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market.top_gainers_or_losers()
    """
    logger.debug(f"Fetching data for top_gainers_or_losers")
    static_options_list = ['gainers', 'loosers']
    validate_param_from_list(to_get, static_options_list)
    data_json = get_top_gainers_or_losers(to_get)
    legends_dict = {item[0]: item[1] for item in data_json['legends']}
    gainers_losers_df = pd.DataFrame()
    for i in legends_dict.keys():
        data_df = pd.DataFrame(data_json[i]['data'])
        data_df['legend'] = i
        if gainers_losers_df.empty:
            gainers_losers_df = data_df
        else:
            gainers_losers_df = pd.concat([gainers_losers_df, data_df])
    return gainers_losers_df


def most_active_equities(fetch_by: str = 'value'):
    """
    to get most active equities fetched by value/volume in live market, after market hour it will get as per last traded value
    link : https://www.nseindia.com/market-data/most-active-equities
    :param fetch_by: select any volume/value
    :return: pandas dataframe
        Example:
            from nselib import capital_market
            df = capital_market.most_active_equities()
    """
    logger.debug(f"Fetching data for most_active_equities")
    static_options_list = ['volume', 'value']
    validate_param_from_list(fetch_by, static_options_list)
    origin_url = "https://www.nseindia.com/market-data/most-active-equities"
    url = f"https://www.nseindia.com/api/live-analysis-most-active-securities?index={fetch_by}"
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
        data_df = pd.DataFrame(data_json['data'])
    except Exception as e:
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    return data_df


def total_traded_stocks():
    """
    to get all total traded stocks detail in live market, after market hour it will get as per last traded value
    summary_dict - has the live market summary for the stocks in dictionary format
    detail_df - has all detail available in the current market in dataframe format
    link : https://www.nseindia.com/market-data/stocks-traded
    :return: summary_dict, detail_df
        Example:
            from nselib import capital_market
            df = capital_market.total_traded_stocks()
    """
    logger.debug(f"Fetching data for total_traded_stocks")
    origin_url = "https://www.nseindia.com/market-data/stocks-traded"
    url = f"https://www.nseindia.com/api/live-analysis-stocksTraded"
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
        summary_dict = data_json['total']['count']
        detail_df = pd.DataFrame(data_json['total']['data'])
    except Exception as e:
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    return summary_dict, detail_df


def _normalize_business_growth_cm_segment_financial_year(from_year, to_year):
    if to_year is None and from_year is not None and "-" in str(from_year):
        from_year, to_year = [part.strip() for part in str(from_year).split("-", 1)]
    if from_year is None or to_year is None:
        raise ValueError(" For monthly data provide from_year and to_year, e.g. from_year='2025', to_year='2026'")
    return str(from_year).strip(), str(to_year).strip()


def _normalize_business_growth_cm_segment_daily_args(month, year):
    if year is None and month is not None and "-" in str(month):
        month, year = [part.strip() for part in str(month).split("-", 1)]
    if month is None or year is None:
        raise ValueError(" For daily data provide month and year, e.g. month='Mar', year='26'")

    month = str(month).strip()
    try:
        month = datetime.strptime(month[:3].title(), "%b").strftime("%b")
    except ValueError as exc:
        raise ValueError(" month should be a valid month name like 'Mar' or 'March'") from exc

    year = str(year).strip()
    if len(year) == 4 and year.isdigit():
        year = year[-2:]
    return month, year


def _business_growth_cm_segment_dataframe(data_json):
    records = [row.get("data", row) for row in data_json.get("data", [])]
    data_df = pd.DataFrame(records)
    if data_df.empty:
        return data_df

    data_df = data_df.replace({r"\r": ""}, regex=True)
    for order_column in ["GTM_MONTH_YEAR_ORDER", "CDT_DATE_ORDER"]:
        if order_column in data_df.columns:
            data_df[order_column] = pd.to_datetime(data_df[order_column], errors="coerce")

    preserve_columns = {"type", "TYPE", "GLY_MONTH_YEAR", "GLM_MONTH_YEAR", "F_TIMESTAMP"}
    null_map = {"": np.nan, "-": np.nan, "--": np.nan, "None": np.nan, "nan": np.nan, "NaN": np.nan}

    for column_name in data_df.columns:
        if column_name in preserve_columns or column_name.endswith("_ORDER"):
            continue
        if pd.api.types.is_datetime64_any_dtype(data_df[column_name]):
            continue
        cleaned_series = data_df[column_name].astype(str).str.replace(",", "", regex=False).str.strip()
        normalized_series = cleaned_series.replace(null_map)
        numeric_series = pd.to_numeric(normalized_series, errors="coerce")
        if numeric_series.notna().sum() == normalized_series.notna().sum():
            data_df[column_name] = numeric_series
        else:
            data_df[column_name] = normalized_series

    meta_keys = [key for key in data_json.keys() if key != "data"]
    if meta_keys:
        data_df.attrs["metadata"] = {key: data_json[key] for key in meta_keys}
    return data_df


def business_growth_cm_segment(data_type: str = "yearly",
                               from_year: str = None,
                               to_year: str = None,
                               month: str = None,
                               year: str = None):
    """
    get historical business growth data for the NSE capital market (CM) segment.
    :param data_type: use one {'yearly', 'monthly', 'daily'}
    :param from_year: required for monthly data. eg: '2025' for FY 2025-2026
    :param to_year: required for monthly data. eg: '2026' for FY 2025-2026
    :param month: required for daily data. eg: 'Mar', 'March' or 'Mar-26'
    :param year: required for daily data. eg: '26' or '2026'
    :return: pandas.DataFrame
    :raise ValueError if the parameter input is not proper
        Example:
            from nselib import capital_market
            df = capital_market._normalize_business_growth_cm_segment_financial_year()
    """
    logger.debug(f"Fetching data for _normalize_business_growth_cm_segment_financial_year")
    static_options_list = ["yearly", "monthly", "daily"]
    validate_param_from_list(data_type, static_options_list)

    if data_type == "yearly":
        data_json = get_business_growth_cm_segment_yearly()
    elif data_type == "monthly":
        from_year, to_year = _normalize_business_growth_cm_segment_financial_year(from_year, to_year)
        data_json = get_business_growth_cm_segment_monthly(from_year=from_year, to_year=to_year)
    else:
        month, year = _normalize_business_growth_cm_segment_daily_args(month, year)
        data_json = get_business_growth_cm_segment_daily(month=month, year=year)

    return _business_growth_cm_segment_dataframe(data_json)


# if __name__ == '__main__':
    # data = pe_ratio(trade_date='23-04-2026')  # trade_date='11-09-2024'
    # data = index_data(index='NIFTY 50', period='1W')
    # data = block_deals_data(period='1W')
    # data = bulk_deal_data(period='1W')
    # data = india_vix_data(period='1W')
    # data = short_selling_data(period='1W')
    # data = index_data(index='NIFTY 50', from_date='21-10-2024', to_date='30-10-2024')
    # data = deliverable_position_data(symbol='SBIN', from_date='23-03-2024', to_date='23-06-2024')
    # data = market_watch_all_indices()
    # data = fno_equity_list()
    # data = price_volume_and_deliverable_position_data(symbol='SBIN',  from_date='23-03-2024', to_date='23-06-2024')
    # data = price_volume_and_deliverable_position_data(symbol='SBIN', period='1M')
    # data = price_volume_data(symbol='SBIN', from_date='20-04-2026', to_date='23-04-2026')
    # data = financial_results_for_equity(from_date='11-03-2025', to_date='16-03-2025', fo_sec=True,
    #                                     fin_period='Quarterly')
    # data = corporate_actions_for_equity(period='6M', fno_only=False)
    # data = event_calendar_for_equity(period='1M', fno_only=False)
    # data = sme_band_complete(trade_date='11-03-2025')
    # data = top_gainers_or_losers('loosers')   # gainers/losers
    # data = most_active_equities(fetch_by='volume')  # value/volume
    # data = total_traded_stocks()
    # data = business_growth_cm_segment(data_type='yearly')
    # data = business_growth_cm_segment(data_type='monthly', from_year='2025', to_year='2026')
    # data = business_growth_cm_segment(data_type='daily', month='Mar', year='26')
    # df = index_data("NIFTY 50", from_date="01-11-2024", to_date="27-12-2024")
    # data = category_turnover_cash(trade_date='11-09-2025')
    # data.to_csv(fr'C:\Users\Quant\Downloads\data.csv')

    # data = niftymidcap150_equity_list()
    # print(data)
    # print(data.info())
    # -----------------------------------------------------


```

#### `nselib/capital_market/get_func.py`

```python
import json
import logging
from io import BytesIO, StringIO

import pandas as pd
import requests

from nselib.constants import india_vix_data_column, index_data_columns
from nselib.errors import NSEdataNotFound
from nselib.libutil import (
    cleaning_column_name,
    default_header,
    derive_from_and_to_date,
    header,
    nse_urlfetch,
    validate_date_param,
)

logger = logging.getLogger(__name__)


def get_price_volume_and_deliverable_position_data(
    symbol: str, from_date: str, to_date: str
) -> pd.DataFrame:
    """
    Fetch price, volume, and deliverable position data for a given symbol and date range.

    Args:
        symbol (str): The NSE symbol (e.g., 'SBIN').
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the price, volume, and deliverable data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_price_volume_and_deliverable_position_data('SBIN', '17-03-2022', '17-06-2023')
    """
    logger.debug(
        f"Fetching price, volume and deliverable data for {symbol} from {from_date} to {to_date}"
    )
    origin_url = "https://nsewebsite-staging.nseindia.com/report-detail/eq_security"
    url = (
        "https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData?"
    )
    payload = f"from={from_date}&to={to_date}&symbol={symbol}&type=priceVolumeDeliverable&series=ALL&csv=true"
    try:
        data_text = nse_urlfetch(url + payload, origin_url=origin_url).text
        data_text = data_text.replace("\x82", "").replace("â¹", "In Rs")
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    data_df = pd.read_csv(StringIO(data_text))
    data_df.columns = [name.replace(" ", "") for name in data_df.columns]
    return data_df


def get_price_volume_data(symbol: str, from_date: str, to_date: str) -> pd.DataFrame:
    """
    Fetch price and volume data for a given symbol and date range.

    Args:
        symbol (str): The NSE symbol (e.g., 'SBIN').
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the price and volume data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_price_volume_data('SBIN', '17-03-2022', '17-06-2023')
    """
    logger.debug(
        f"Fetching price and volume data for {symbol} from {from_date} to {to_date}"
    )
    origin_url = "https://nsewebsite-staging.nseindia.com/report-detail/eq_security"
    url = (
        "https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData?"
    )
    payload = f"from={from_date}&to={to_date}&symbol={symbol}&type=priceVolume&series=ALL&csv=true"
    try:
        data_text = nse_urlfetch(url + payload, origin_url=origin_url)
        if data_text.status_code != 200:
            raise NSEdataNotFound(f" Resource not available for Price Volume Data")
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    data_df = pd.read_csv(BytesIO(data_text.content), index_col=False)
    data_df.columns = [name.replace(" ", "") for name in data_df.columns]
    return data_df


def get_deliverable_position_data(
    symbol: str, from_date: str, to_date: str
) -> pd.DataFrame:
    """
    Fetch deliverable position data for a given symbol and date range.

    Args:
        symbol (str): The NSE symbol (e.g., 'SBIN').
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the deliverable position data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_deliverable_position_data('SBIN', '17-03-2022', '17-06-2023')
    """
    logger.debug(
        f"Fetching deliverable position data for {symbol} from {from_date} to {to_date}"
    )
    origin_url = "https://nsewebsite-staging.nseindia.com/report-detail/eq_security"
    url = (
        "https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData?"
    )
    payload = f"from={from_date}&to={to_date}&symbol={symbol}&type=deliverable&series=ALL&csv=true"
    try:
        data_text = nse_urlfetch(url + payload, origin_url=origin_url)
        if data_text.status_code != 200:
            raise NSEdataNotFound(
                "Resource not available for deliverable_position_data"
            )
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    data_df = pd.read_csv(BytesIO(data_text.content), index_col=False)
    data_df.columns = [name.replace(" ", "") for name in data_df.columns]
    return data_df


def get_india_vix_data(from_date: str, to_date: str) -> pd.DataFrame:
    """
    Fetch India VIX data for a given date range.

    Args:
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the India VIX data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_india_vix_data('17-03-2022', '17-06-2023')
    """
    logger.debug(f"Fetching India VIX data from {from_date} to {to_date}")
    origin_url = "https://nsewebsite-staging.nseindia.com/report-detail/eq_security"
    url = f"https://www.nseindia.com/api/historicalOR/vixhistory?from={from_date}&to={to_date}&csv=true"
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
        data_df = pd.DataFrame(data_json["data"])
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    # data_df.drop(columns='TIMESTAMP', inplace=True)
    if not data_df.empty:
        data_df.columns = cleaning_column_name(data_df.columns)
        return data_df[india_vix_data_column]
    return data_df


def get_index_data(index: str, from_date: str, to_date: str) -> pd.DataFrame:
    """
    Fetch index data for a given index and date range.

    Args:
        index (str): The index name (e.g., 'NIFTY 50').
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the historical index data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_index_data('NIFTY 50', '17-03-2022', '17-06-2023')
    """
    logger.debug(f"Fetching index data for {index} from {from_date} to {to_date}")
    index = index.replace(" ", "%20").upper()
    origin_url = "https://www.nseindia.com/reports-indices-historical-index-data"
    url = f"https://www.nseindia.com/api/historicalOR/indicesHistory?indexType={index}&from={from_date}&to={to_date}"
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
        data_df = pd.DataFrame(data_json["data"])
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    if not data_df.empty:
        data_df.drop(columns="HI_TIMESTAMP", inplace=True)
        data_df.columns = index_data_columns
    return data_df


def get_bulk_deal_data(from_date: str, to_date: str) -> pd.DataFrame:
    """
    Fetch bulk deal data for a given date range.

    Args:
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the bulk deal data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_bulk_deal_data('17-03-2022', '17-06-2023')
    """
    logger.debug(f"Fetching bulk deal data from {from_date} to {to_date}")
    origin_url = "https://nsewebsite-staging.nseindia.com"
    url = "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=bulk_deals&"
    payload = f"from={from_date}&to={to_date}&csv=true"
    data_text = nse_urlfetch(url + payload, origin_url=origin_url)
    if data_text.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for bulk_deal_data")
    data_df = pd.read_csv(BytesIO(data_text.content), index_col=False)
    data_df.columns = [name.replace(" ", "") for name in data_df.columns]
    return data_df


def get_block_deals_data(from_date: str, to_date: str) -> pd.DataFrame:
    """
    Fetch block deals data for a given date range.

    Args:
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the block deals data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_block_deals_data('17-03-2022', '17-06-2023')
    """
    logger.debug(f"Fetching block deals data from {from_date} to {to_date}")
    origin_url = "https://nsewebsite-staging.nseindia.com"
    url = "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=block_deals&"
    payload = f"from={from_date}&to={to_date}&csv=true"
    data_text = nse_urlfetch(url + payload, origin_url=origin_url)
    if data_text.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for block_deals_data")
    data_df = pd.read_csv(BytesIO(data_text.content), index_col=False)
    data_df.columns = [name.replace(" ", "") for name in data_df.columns]
    return data_df


def get_short_selling_data(from_date: str, to_date: str) -> pd.DataFrame:
    """
    Fetch NSE short selling data in a data frame.

    Args:
        from_date (str): Start date in 'dd-mm-YYYY' format.
        to_date (str): End date in 'dd-mm-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the short selling data.

    Example:
            from nselib import capital_market
            df = capital_market.get_func.get_short_selling_data('17-03-2022', '17-06-2023')
    """
    logger.debug(f"Fetching short selling data from {from_date} to {to_date}")
    origin_url = "https://nsewebsite-staging.nseindia.com"
    url = "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=short_selling&"
    payload = f"from={from_date}&to={to_date}&csv=true"
    data_text = nse_urlfetch(url + payload, origin_url=origin_url)
    if data_text.status_code != 200:
        raise NSEdataNotFound(f" Resource not available for short_selling_data")
    data_df = pd.read_csv(BytesIO(data_text.content), index_col=False)
    data_df.columns = [name.replace(" ", "") for name in data_df.columns]
    return data_df


def _get_business_growth_cm_segment_data(api_path: str) -> dict:
    """
    Internal helper to fetch business growth CM segment data.

    Args:
        api_path (str): The specific API path to fetch.

    Returns:
        dict: Parsed JSON data.
    """
    logger.debug(f"Fetching business growth CM segment data from path: {api_path}")
    origin_url = "https://www.nseindia.com/market-data/business-growth-cm-segment"
    url = f"https://www.nseindia.com{api_path}"
    try:
        r_session = requests.session()
        r_session.trust_env = False
        nse_live = r_session.get(origin_url, headers=default_header)
        cookies = nse_live.cookies
        data_json = r_session.get(url, headers=header, cookies=cookies).json()
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    return data_json


def get_business_growth_cm_segment_yearly() -> dict:
    """
    Fetch yearly business growth data for the CM segment.

    Returns:
        dict: Parsed JSON response.

    Example:
            from nselib import capital_market
            data = capital_market.get_func.get_business_growth_cm_segment_yearly()
    """
    logger.debug("Fetching yearly business growth CM segment data")
    return _get_business_growth_cm_segment_data("/api/historicalOR/cm/tbg/yearly")


def get_business_growth_cm_segment_monthly(from_year: str, to_year: str) -> dict:
    """
    Fetch monthly business growth data for the CM segment for a specific year range.

    Args:
        from_year (str): The starting year (e.g., '2022').
        to_year (str): The ending year (e.g., '2023').

    Returns:
        dict: Parsed JSON response.

    Example:
            from nselib import capital_market
            data = capital_market.get_func.get_business_growth_cm_segment_monthly('2022', '2023')
    """
    logger.debug(
        f"Fetching monthly business growth CM segment data for {from_year}-{to_year}"
    )
    return _get_business_growth_cm_segment_data(
        f"/api/historicalOR/cm/tbg/monthly?from={from_year}&to={to_year}"
    )


def get_business_growth_cm_segment_daily(month: str, year: str) -> dict:
    """
    Fetch daily business growth data for the CM segment for a specific month and year.

    Args:
        month (str): The month abbreviation (e.g., 'Mar').
        year (str): The year (e.g., '2023').

    Returns:
        dict: Parsed JSON response.

    Example:
            from nselib import capital_market
            data = capital_market.get_func.get_business_growth_cm_segment_daily('Mar', '2023')
    """
    logger.debug(f"Fetching daily business growth CM segment data for {month}-{year}")
    return _get_business_growth_cm_segment_data(
        f"/api/historicalOR/cm/tbg/daily?month={month}&year={year}"
    )


def get_financial_results_master(
    from_date: str = None,
    to_date: str = None,
    period: str = None,
    fo_sec: bool = False,
    fin_period: str = "Quarterly",
) -> tuple:
    """
    Fetch corporate financial results master data.

    Args:
        from_date (str, optional): Start date in 'dd-mm-YYYY' format.
        to_date (str, optional): End date in 'dd-mm-YYYY' format.
        period (str, optional): Period like '1M', '1Y'.
        fo_sec (bool, optional): Whether to fetch for F&O securities.
        fin_period (str, optional): Financial period (e.g., 'Quarterly').

    Returns:
        tuple: (master_data_df, headers, ns, keys_to_extract)

    Example:
            from nselib import capital_market
            master_df, hdrs, ns, keys = capital_market.get_func.get_financial_results_master(period='1M')
    """
    logger.debug(
        f"Fetching financial results master for period={period}, from={from_date}, to={to_date}"
    )
    validate_date_param(from_date, to_date, period)
    from_date, to_date = derive_from_and_to_date(
        from_date=from_date, to_date=to_date, period=period
    )
    origin_url = (
        "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
    )
    url_ = "https://www.nseindia.com/api/corporates-financial-results?index=equities&"
    if fo_sec:
        payload = (
            f"from_date={from_date}&to_date={to_date}&fo_sec=true&period={fin_period}"
        )
    else:
        payload = f"from_date={from_date}&to_date={to_date}&period={fin_period}"
    data_text = nse_urlfetch(url_ + payload, origin_url=origin_url)
    if data_text.status_code != 200:
        raise NSEdataNotFound(
            f" Resource not available for financial data with these parameters"
        )
    json_str = data_text.content.decode("utf-8")
    data_list = json.loads(json_str)
    master_data_df = pd.DataFrame(data_list)
    master_data_df.columns = [name.replace(" ", "") for name in master_data_df.columns]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }
    ns = {
        "xbrli": "http://www.xbrl.org/2003/instance",
        "in-bse-fin": "http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin",
    }
    keys_to_extract = [
        "ScripCode",
        "Symbol",
        "MSEISymbol",
        "NameOfTheCompany",
        "ClassOfSecurity",
        "DateOfStartOfFinancialYear",
        "DateOfEndOfFinancialYear",
        "DateOfBoardMeetingWhenFinancialResultsWereApproved",
        "DateOnWhichPriorIntimationOfTheMeetingForConsideringFinancialResultsWasInformedToTheExchange",
        "DescriptionOfPresentationCurrency",
        "LevelOfRoundingUsedInFinancialStatements",
        "ReportingQuarter",
        "StartTimeOfBoardMeeting",
        "EndTimeOfBoardMeeting",
        "DateOfStartOfBoardMeeting",
        "DateOfEndOfBoardMeeting",
        "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification",
        "IsCompanyReportingMultisegmentOrSingleSegment",
        "DescriptionOfSingleSegment",
        "DateOfStartOfReportingPeriod",
        "DateOfEndOfReportingPeriod",
        "WhetherResultsAreAuditedOrUnaudited",
        "NatureOfReportStandaloneConsolidated",
        "RevenueFromOperations",
        "OtherIncome",
        "Income",
        "CostOfMaterialsConsumed",
        "PurchasesOfStockInTrade",
        "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
        "EmployeeBenefitExpense",
        "FinanceCosts",
        "DepreciationDepletionAndAmortisationExpense",
        "OtherExpenses",
        "Expenses",
        "ProfitBeforeExceptionalItemsAndTax",
        "ExceptionalItemsBeforeTax",
        "ProfitBeforeTax",
        "CurrentTax",
        "DeferredTax",
        "TaxExpense",
        "NetMovementInRegulatoryDeferralAccountBalancesRelatedToProfitOrLossAndTheRelatedDeferredTaxMovement",
        "ProfitLossForPeriodFromContinuingOperations",
        "ProfitLossFromDiscontinuedOperationsBeforeTax",
        "TaxExpenseOfDiscontinuedOperations",
        "ProfitLossFromDiscontinuedOperationsAfterTax",
        "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
        "ProfitLossForPeriod",
        "OtherComprehensiveIncomeNetOfTaxes",
        "ComprehensiveIncomeForThePeriod",
        "ProfitOrLossAttributableToOwnersOfParent",
        "ProfitOrLossAttributableToNonControllingInterests",
        "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent",
        "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParentNonControllingInterests",
        "PaidUpValueOfEquityShareCapital",
        "FaceValueOfEquityShareCapital",
        "BasicEarningsLossPerShareFromContinuingOperations",
        "DilutedEarningsLossPerShareFromContinuingOperations",
        "BasicEarningsLossPerShareFromDiscontinuedOperations",
        "DilutedEarningsLossPerShareFromDiscontinuedOperations",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "DescriptionOfOtherExpenses",
        "OtherExpenses",
        "DescriptionOfItemThatWillNotBeReclassifiedToProfitAndLoss",
        "AmountOfItemThatWillNotBeReclassifiedToProfitAndLoss",
        "IncomeTaxRelatingToItemsThatWillNotBeReclassifiedToProfitOrLoss",
        "DescriptionOfItemThatWillBeReclassifiedToProfitAndLoss",
        "AmountOfItemThatWillBeReclassifiedToProfitAndLoss",
        "IncomeTaxRelatingToItemsThatWillBeReclassifiedToProfitOrLoss",
    ]
    return master_data_df, headers, ns, keys_to_extract


def get_top_gainers_or_losers(to_get: str) -> dict:
    """
    Fetch top gainers or losers for a given index.

    Args:
        to_get (str): Index to fetch (e.g., 'NIFTY 50').

    Returns:
        dict: Parsed JSON data.

    Example:
            from nselib import capital_market
            data = capital_market.get_func.get_top_gainers_or_losers('NIFTY 50')
    """
    logger.debug(f"Fetching top gainers or losers for index: {to_get}")
    origin_url = "https://www.nseindia.com/market-data/top-gainers-losers"
    url = f"https://www.nseindia.com/api/live-analysis-variations?index={to_get}"
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    return data_json

```

#### `nselib/cash_market/__init__.py`

```python
from .cash_market_data import (
    amfi_monthly_data,
    amfi_monthly_historical_data,
    amfi_monthly_report_links,
    nsdl_fpi_derivative_activity,
    nsdl_fpi_investment_activity,
    nsdl_fpi_latest_derivative_activity,
    nsdl_fpi_latest_investment_activity,
)


```

#### `nselib/cash_market/cash_market_data.py`

```python
from __future__ import annotations

from datetime import date

from nselib.mutual_funds import (
    amfi_monthly_data as _amfi_monthly_data,
    amfi_monthly_historical_data as _amfi_monthly_historical_data,
    amfi_monthly_report_links as _amfi_monthly_report_links,
)
from nselib.nsdl_fpi import (
    fetch_nsdl_fpi_derivative_activity,
    fetch_nsdl_fpi_investment_activity,
    fetch_nsdl_fpi_latest_derivative_activity,
    fetch_nsdl_fpi_latest_investment_activity,
)


def nsdl_fpi_investment_activity(trade_date: str):
    """
    NSDL FPI investment activity for the traded date provided.
    :param trade_date: eg:'30-10-2025'
    :return: pandas dataframe
    """
    return fetch_nsdl_fpi_investment_activity(trade_date)


def nsdl_fpi_latest_investment_activity():
    """
    Latest NSDL FPI investment activity.
    :return: pandas dataframe
    """
    return fetch_nsdl_fpi_latest_investment_activity()


def nsdl_fpi_derivative_activity(trade_date: str):
    """
    NSDL FPI derivative activity for the traded date provided.
    :param trade_date: eg:'30-10-2025'
    :return: pandas dataframe
    """
    return fetch_nsdl_fpi_derivative_activity(trade_date)


def nsdl_fpi_latest_derivative_activity():
    """
    Latest NSDL FPI derivative activity.
    :return: pandas dataframe
    """
    return fetch_nsdl_fpi_latest_derivative_activity()


def amfi_monthly_report_links():
    """
    List all report links exposed on the AMFI monthly archive page.
    """
    return _amfi_monthly_report_links()


def amfi_monthly_data(
    report_month: str | date,
    file_type_priority: tuple[str, ...] | list[str] | None = None,
):
    """
    Fetch a single month AMFI report and return normalized row-wise records.
    """
    return _amfi_monthly_data(
        report_month=report_month,
        file_type_priority=file_type_priority,
    )


def amfi_monthly_historical_data(
    from_month: str | date | None = None,
    to_month: str | date | None = None,
    file_type_priority: tuple[str, ...] | list[str] | None = None,
    include_all_file_variants: bool = False,
    strict: bool = False,
):
    """
    Fetch AMFI monthly reports for a historical month range.
    """
    return _amfi_monthly_historical_data(
        from_month=from_month,
        to_month=to_month,
        file_type_priority=file_type_priority,
        include_all_file_variants=include_all_file_variants,
        strict=strict,
    )

```

#### `nselib/constants.py`

```python

dd_mm_yyyy = '%d-%m-%Y'
dd_mmm_yyyy = '%d-%b-%Y'
ddmmyyyy = '%d%m%Y'
ddmmyy = '%d%m%y'
mmm_yy = '%b-%y'

equity_periods = ['1D', '1W', '1M', '3M', '6M', '1Y']
indices_list = ['NIFTY', 'FINNIFTY', 'BANKNIFTY']


# ---------- column lists-----------------

price_volume_and_deliverable_position_data_columns = \
    ['Symbol', 'Series', 'Date', 'PrevClose', 'OpenPrice', 'HighPrice',
     'LowPrice', 'LastPrice', 'ClosePrice', 'AveragePrice', 'TotalTradedQuantity',
     'TurnoverInRs', 'No.ofTrades', 'DeliverableQty', '%DlyQttoTradedQty']

price_volume_data_columns = ['Symbol', 'Series', 'Date', 'PrevClose', 'OpenPrice', 'HighPrice',
                             'LowPrice', 'LastPrice', 'ClosePrice', 'AveragePrice',
                             'TotalTradedQuantity', 'Turnover', 'No.ofTrades']

deliverable_data_columns = ['Symbol', 'Series', 'Date', 'TradedQty', 'DeliverableQty', '%DlyQttoTradedQty']

bulk_deal_data_columns = ['Date', 'Symbol', 'SecurityName', 'ClientName', 'Buy/Sell', 'QuantityTraded',
                          'TradePrice/Wght.Avg.Price', 'Remarks']

block_deals_data_columns = ['Date', 'Symbol', 'SecurityName', 'ClientName', 'Buy/Sell', 'QuantityTraded',
                            'TradePrice/Wght.Avg.Price', 'Remarks']

short_selling_data_columns = ['Date', 'Symbol', 'SecurityName', 'Quantity']

bhavcopy_old = ['TradDt', 'ISIN', 'TckrSymb', 'SctySrs', 'OpnPric', 'HghPric', 'LwPric', 'ClsPric', 'LastPric',
                'PrvsClsgPric', 'TtlTradgVol', 'TtlTrfVal', 'TtlNbOfTxsExctd']

bhavcopy_new = ['TIMESTAMP', 'ISIN', 'SYMBOL', 'SERIES', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'LAST', 'PREVCLOSE',
                'TOTTRDQTY', 'TOTTRDVAL', 'TOTALTRADES']

future_price_volume_data_column = ['TIMESTAMP', 'INSTRUMENT', 'SYMBOL', 'EXPIRY_DT', 'STRIKE_PRICE', 'OPTION_TYPE', 'MARKET_TYPE',
                                   'OPENING_PRICE', 'TRADE_HIGH_PRICE', 'TRADE_LOW_PRICE', 'CLOSING_PRICE',
                                   'LAST_TRADED_PRICE', 'PREV_CLS', 'SETTLE_PRICE', 'TOT_TRADED_QTY', 'TOT_TRADED_VAL',
                                   'OPEN_INT', 'CHANGE_IN_OI', 'MARKET_LOT', 'UNDERLYING_VALUE']

india_vix_data_column = ['TIMESTAMP', 'INDEX_NAME', 'OPEN_INDEX_VAL', 'CLOSE_INDEX_VAL', 'HIGH_INDEX_VAL',
                         'LOW_INDEX_VAL', 'PREV_CLOSE', 'VIX_PTS_CHG', 'VIX_PERC_CHG']

index_data_columns = ['INDEX_NAME', 'OPEN_INDEX_VAL', 'HIGH_INDEX_VAL', 'CLOSE_INDEX_VAL',
                      'LOW_INDEX_VAL', 'TURN_OVER', 'TRADED_QTY', 'TIMESTAMP']

var_columns = ['RecordType', 'Symbol', 'Series', 'Isin', 'SecurityVaR', 'IndexVaR', 'VaRMargin',
               'ExtremeLossRate', 'AdhocMargin', 'ApplicableMarginRate']

```

#### `nselib/debt/__init__.py`

```python
from .debt_data import securities_available_for_trading

```

#### `nselib/debt/debt_data.py`

```python
import datetime as dt
import logging
from io import BytesIO

import pandas as pd

from nselib.libutil import nse_urlfetch

logger = logging.getLogger(__name__)


def securities_available_for_trading(trade_date: str) -> pd.DataFrame:
    """
    Get securities available for trading.

    Args:
        trade_date (str): Date in 'dd-mm-YYYY' format (e.g., '17-03-2022').

    Returns:
        pandas.DataFrame: A DataFrame containing securities available for trading.

    Example:
            from nselib import debt
            df = debt.securities_available_for_trading('17-03-2022')
    """
    logger.debug(f"Fetching securities available for trading for date: {trade_date}")
    month = dt.datetime.strptime(trade_date, "%d-%m-%Y").strftime("%b").upper()
    year = dt.datetime.strptime(trade_date, "%d-%m-%Y").strftime("%Y")
    date_str = dt.datetime.strptime(trade_date, "%d-%m-%Y").strftime("%d%m%Y")
    origin_url = "https://www.nseindia.com/all-reports-debt"
    url = f"https://nsearchives.nseindia.com/content/historical/WDM/{year}/{month}/wdmlist_{date_str}.csv"
    file_chk = nse_urlfetch(url, origin_url=origin_url)
    if file_chk.status_code != 200:
        logger.error(f"Failed to fetch data, status code: {file_chk.status_code}")
        raise FileNotFoundError("No data available")
    try:
        data_df = pd.read_csv(BytesIO(file_chk.content))
    except Exception as e:
        logger.error(f"Failed to parse equity list: {e}", exc_info=e)
        raise FileNotFoundError(f" Equity List not found :: NSE error : {e}")

    return data_df

```

#### `nselib/derivatives/__init__.py`

```python
from .derivative_data import future_price_volume_data, option_price_volume_data, fno_bhav_copy, \
    participant_wise_open_interest, participant_wise_trading_volume, expiry_dates_future, expiry_dates_option_index,\
    nse_live_option_chain, fii_derivatives_statistics, fno_security_in_ban_period, live_most_active_underlying, \
    daily_volatility, category_turnover_fo, \
    business_growth_fo_segment

```

#### `nselib/derivatives/derivative_data.py`

```python
from nselib.libutil import header
from nselib.libutil import default_header
import logging
import numpy as np
import pandas as pd
import zipfile
import requests
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

from nselib.constants import ddmmyy
from nselib.derivatives.get_func import (
    cleaning_nse_symbol,
    dd_mm_yyyy,
    derive_from_and_to_date,
    future_price_volume_data_column,
    get_business_growth_fo_segment_daily,
    get_business_growth_fo_segment_monthly,
    get_business_growth_fo_segment_yearly,
    get_future_price_volume_data,
    get_nse_option_chain,
    get_option_price_volume_data,
    indices_list,
    nse_urlfetch,
    validate_date_param,
    validate_param_from_list,
)
from nselib.errors import (
    DerivativeInstrumentNotFoundError,
    NSEdataNotFound,
    NSEApiError,
)

logger = logging.getLogger(__name__)


def future_price_volume_data(
    symbol: str,
    instrument: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    period: Optional[str] = None,
):
    """
    Fetch the contract-wise future price and volume data set.

    Args:
        symbol (str): The NSE symbol (e.g., 'SBIN' or 'BANKNIFTY').
        instrument (str): The instrument type (e.g., 'FUTIDX' or 'FUTSTK').
        from_date (str, optional): The start date in 'dd-mm-YYYY' format (e.g., '17-03-2022').
        to_date (str, optional): The end date in 'dd-mm-YYYY' format (e.g., '17-06-2023').
        period (str, optional): A predefined time period to fetch data for.
            Must be one of {'1D': last day, '1W': last 7 days, '1M': last month, '3M': last 3 months, '6M': last 6 months}.

    Returns:
        pd.DataFrame: A DataFrame containing the future price volume data.

    Raises:
        ValueError: If the parameters or dates are formatted incorrectly.

    Example:
            from nselib import derivatives
            df = derivatives.future_price_volume_data(symbol='SBIN', instrument='FUTSTK', period='1M')
    """
    validate_date_param(from_date, to_date, period)
    logger.debug(
        f"Fetching future price volume data (aggregated) for symbol: {symbol}, instrument: {instrument}, period: {period}"
    )
    symbol, instrument = cleaning_nse_symbol(symbol=symbol), instrument.upper()
    if instrument not in ["FUTIDX", "FUTSTK"]:
        raise DerivativeInstrumentNotFoundError(
            f"{instrument} is not a future instrument"
        )

    from_date, to_date = derive_from_and_to_date(
        from_date=from_date, to_date=to_date, period=period
    )
    nse_df = pd.DataFrame(columns=future_price_volume_data_column)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 90:
            end_date = (from_date + timedelta(90)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        data_df = get_future_price_volume_data(
            symbol=symbol, instrument=instrument, from_date=start_date, to_date=end_date
        )
        from_date = from_date + timedelta(91)
        load_days = (to_date - from_date).days
        if nse_df.empty:
            nse_df = data_df
        else:
            nse_df = pd.concat([nse_df, data_df], ignore_index=True)
    logger.debug(f"Aggregated {len(nse_df)} records for {symbol} futures.")
    return nse_df


def option_price_volume_data(
    symbol: str,
    instrument: str,
    option_type: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    period: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch the contract-wise option price and volume data set.
    Note: Collecting more than 90 days of data may take a significantly longer time.

    Args:
        symbol (str): The NSE symbol (e.g., 'SBIN' or 'BANKNIFTY').
        instrument (str): The instrument type (e.g., 'OPTIDX' or 'OPTSTK').
        option_type (str, optional): The option type (e.g., 'PE' or 'CE').
        from_date (str, optional): The start date in 'dd-mm-YYYY' format.
        to_date (str, optional): The end date in 'dd-mm-YYYY' format.
        period (str, optional): A predefined time period to fetch data for.
            Must be one of {'1D': last day, '1W': last 7 days, '1M': last month, '3M': last 3 months, '6M': last 6 months}.

    Returns:
        pd.DataFrame: A DataFrame containing the option price volume data.

    Raises:
        ValueError: If the parameters or dates are formatted incorrectly.

    Example:
            from nselib import derivatives
            df = derivatives.option_price_volume_data(symbol='NIFTY', instrument='OPTIDX', option_type='CE', period='1M')
    """
    validate_date_param(from_date, to_date, period)
    logger.debug(
        f"Fetching option price volume data (aggregated) for symbol: {symbol}, instrument: {instrument}, option_type: {option_type}"
    )
    symbol, instrument = cleaning_nse_symbol(symbol=symbol), instrument.upper()
    if instrument not in ["OPTIDX", "OPTSTK"]:
        raise DerivativeInstrumentNotFoundError(
            f"{instrument} is not a future instrument"
        )

    if option_type and option_type not in ["PE", "CE"]:
        raise DerivativeInstrumentNotFoundError(
            f"{option_type} is not a valid option type"
        )

    option_type = [option_type] if option_type else ["PE", "CE"]
    from_date, to_date = derive_from_and_to_date(
        from_date=from_date, to_date=to_date, period=period
    )
    nse_df = pd.DataFrame(columns=future_price_volume_data_column)
    from_date = datetime.strptime(from_date, dd_mm_yyyy)
    to_date = datetime.strptime(to_date, dd_mm_yyyy)
    load_days = (to_date - from_date).days
    while load_days > 0:
        if load_days > 90:
            end_date = (from_date + timedelta(90)).strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        else:
            end_date = to_date.strftime(dd_mm_yyyy)
            start_date = from_date.strftime(dd_mm_yyyy)
        for opt_typ in option_type:
            data_df = get_option_price_volume_data(
                symbol=symbol,
                instrument=instrument,
                option_type=opt_typ,
                from_date=start_date,
                to_date=end_date,
            )
            if nse_df.empty:
                nse_df = data_df
            else:
                nse_df = pd.concat([nse_df, data_df], ignore_index=True)
        from_date = from_date + timedelta(91)
        load_days = (to_date - from_date).days

    logger.debug(f"Aggregated {len(nse_df)} records for {symbol} options.")
    return nse_df


def fno_bhav_copy(trade_date: str) -> pd.DataFrame:
    """
    Fetch the new CM-UDiFF Common NSE future and options bhav copy.
    Valid from 2018 onwards.

    Args:
        trade_date (str): The trade date in 'dd-mm-YYYY' format (e.g., '20-06-2023').

    Returns:
        pd.DataFrame: A DataFrame containing the F&O Bhavcopy data.

    Raises:
        NSEdataNotFound: If no data is found for the given date.

    Example:
            from nselib import derivatives
            df = derivatives.fno_bhav_copy(trade_date='17-02-2025')
    """
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    logger.debug(
        f"Fetching F&O Bhavcopy for trade date: {trade_date.strftime('%d-%m-%Y')}"
    )
    url = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_"
    payload = f"{str(trade_date.strftime('%Y%m%d'))}_F_0000.csv.zip"
    request_bhav = nse_urlfetch(url + payload)
    bhav_df = pd.DataFrame()
    if request_bhav.status_code == 200:
        zip_bhav = zipfile.ZipFile(BytesIO(request_bhav.content), "r")
        for file_name in zip_bhav.filelist:
            if file_name:
                bhav_df = pd.read_csv(zip_bhav.open(file_name))
    elif request_bhav.status_code == 403:
        url2 = (
            "https://www.nseindia.com/api/reports?archives="
            "%5B%7B%22name%22%3A%22F%26O%20-%20Bhavcopy(csv)%22%2C%22type%22%3A%22archives%22%2C%22category%22"
            f"%3A%22derivatives%22%2C%22section%22%3A%22equity%22%7D%5D&date={str(trade_date.strftime('%d-%b-%Y'))}"
            f"&type=equity&mode=single"
        )
        request_bhav = nse_urlfetch(url2)
        if request_bhav.status_code == 200:
            zip_bhav = zipfile.ZipFile(BytesIO(request_bhav.content), "r")
            for file_name in zip_bhav.filelist:
                if file_name:
                    bhav_df = pd.read_csv(zip_bhav.open(file_name))
        elif request_bhav.status_code == 403:
            logger.error(
                f"F&O Bhavcopy data not found for {trade_date.strftime('%d-%m-%Y')}"
            )
            raise NSEdataNotFound("Data not found, change the date...")
    # bhav_df = bhav_df[['INSTRUMENT', 'SYMBOL', 'EXPIRY_DT', 'STRIKE_PR', 'OPTION_TYP', 'OPEN', 'HIGH', 'LOW',
    #                    'CLOSE', 'SETTLE_PR', 'CONTRACTS', 'VAL_INLAKH', 'OPEN_INT', 'CHG_IN_OI', 'TIMESTAMP']]
    logger.debug(f"Successfully retrieved F&O Bhavcopy with {len(bhav_df)} records.")
    return bhav_df


def participant_wise_open_interest(trade_date: str) -> pd.DataFrame:
    """
    Fetch FII, DII, Pro, and Client-wise participant Open Interest (OI) data for a given trade date.

    Args:
        trade_date (str): The trade date in 'dd-mm-YYYY' format (e.g., '20-06-2023').

    Returns:
        pd.DataFrame: A DataFrame containing participant-wise open interest data.

    Raises:
        NSEdataNotFound: If the OI data is not available for the given date.

    Example:
            from nselib import derivatives
            df = derivatives.participant_wise_open_interest(trade_date='16-09-2024')
    """
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    logger.debug(
        f"Fetching participant-wise open interest for trade date: {trade_date.strftime('%d-%m-%Y')}"
    )
    url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{str(trade_date.strftime('%d%m%Y'))}.csv"
    # payload = f"{str(for_date.strftime('%d%m%Y'))}.csv"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code == 404:
        url = f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{str(trade_date.strftime('%d%m%Y'))}.csv"
        file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        logger.error(
            f"Participant-wise open interest data not found for {trade_date.strftime('%d-%m-%Y')}"
        )
        raise NSEdataNotFound(f"No data available for : {trade_date}")
    try:
        # data_df = pd.read_csv(url, engine='python', sep=',', quotechar='"', on_bad_lines='skip', skiprows=1)
        data_df = pd.read_csv(
            BytesIO(file_chk.content), on_bad_lines="skip", skiprows=1
        )
    except Exception as e:
        logger.error(
            f"Error while fetching participant wise open interest data: {e}",
            exc_info=e,
        )
        data_df = pd.read_csv(
            BytesIO(file_chk.content), on_bad_lines="skip", skiprows=1
        )
        data_df.drop(data_df.tail(1).index, inplace=True)
        data_df.columns = [name.replace("\t", "") for name in data_df.columns]
    return data_df


def participant_wise_trading_volume(trade_date: str) -> pd.DataFrame:
    """
    Fetch FII, DII, Pro, and Client-wise participant trading volume data for a given trade date.

    Args:
        trade_date (str): The trade date in 'dd-mm-YYYY' format (e.g., '20-06-2023').

    Returns:
        pd.DataFrame: A DataFrame containing participant-wise trading volume data.

    Raises:
        NSEdataNotFound: If the volume data is not available for the given date.

    Example:
            from nselib import derivatives
            df = derivatives.participant_wise_trading_volume(trade_date='16-09-2024')
    """
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    logger.debug(
        f"Fetching participant-wise trading volume for trade date: {trade_date.strftime('%d-%m-%Y')}"
    )
    url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{str(trade_date.strftime('%d%m%Y'))}.csv"
    # payload = f"{str(for_date.strftime('%d%m%Y'))}.csv"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        logger.error(
            f"Participant-wise trading volume data not found for {trade_date.strftime('%d-%m-%Y')}"
        )
        raise NSEdataNotFound(f"No data available for : {trade_date}")
    try:
        data_df = pd.read_csv(
            BytesIO(file_chk.content), on_bad_lines="skip", skiprows=1
        )
    except Exception as e:
        logger.error(
            f"Error while fetching participant wise trading volume data: {e}",
            exc_info=e,
        )
        data_df = pd.read_csv(
            BytesIO(file_chk.content),
            engine="c",
            sep=",",
            quotechar='"',
            on_bad_lines="skip",
            skiprows=1,
        )
        data_df.drop(data_df.tail(1).index, inplace=True)
        data_df.columns = [name.replace("\t", "") for name in data_df.columns]
    return data_df


def daily_volatility(trade_date: str):
    """
    get F&O daily volatility report as per the traded date provided
    :param trade_date: eg:'20-06-2023'
    :return: pandas dataframe
    """
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    payload = f"FOVOLT_{str(trade_date.strftime('%d%m%Y'))}.csv"
    origin_url = "https://www.nseindia.com/all-reports-derivatives"
    report_urls = [
        f"https://nsearchives.nseindia.com/archives/nsccl/volt/{payload}",
        f"https://archives.nseindia.com/archives/nsccl/volt/{payload}",
        "https://www.nseindia.com/api/reports?archives="
        "%5B%7B%22name%22%3A%22F%26O%20-%20Daily%20Volatility%22%2C%22type%22%3A%22archives%22%2C%22category%22"
        f"%3A%22derivatives%22%2C%22section%22%3A%22equity%22%7D%5D&date={str(trade_date.strftime('%d-%b-%Y'))}"
        f"&type=equity&mode=single",
    ]
    last_status_code = None
    for url in report_urls:
        # NSE archive requests can fail behind stale local proxy settings, so fetch this report
        # with a session that ignores environment proxies while preserving the usual cookie flow.
        r_session = requests.session()
        r_session.trust_env = False
        nse_live = r_session.get(origin_url, headers=default_header)
        cookies = nse_live.cookies
        report = r_session.get(url, headers=header, cookies=cookies)
        last_status_code = report.status_code
        if report.status_code != 200:
            continue
        try:
            data_df = pd.read_csv(BytesIO(report.content), skipinitialspace=True)
        except Exception as exc:
            raise FileNotFoundError(
                f" Daily volatility data not found for : {trade_date.strftime(dd_mm_yyyy)} :: NSE error : {exc}"
            )
        data_df = data_df.dropna(how="all")
        data_df.columns = [column_name.strip() for column_name in data_df.columns]
        return data_df
    raise FileNotFoundError(
        f" No daily volatility data available for : {trade_date.strftime(dd_mm_yyyy)} :: status={last_status_code}"
    )


def category_turnover_fo(trade_date: str) -> pd.DataFrame:
    """
    Fetch NSE derivatives category-wise turnover data for a given trade date.

    Args:
        trade_date (str): The trade date in 'dd-mm-YYYY' format (e.g., '07-04-2026').

    Returns:
        pd.DataFrame: A DataFrame containing the category-wise turnover data.

    Raises:
        NSEdataNotFound: If the turnover data cannot be found for the given date.

    Example:
            from nselib import derivatives
            df = derivatives.category_turnover_fo(trade_date='16-09-2025')
    """
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    logger.debug(
        f"Fetching category-wise turnover for trade date: {trade_date.strftime('%d-%m-%Y')}"
    )
    url = f"https://archives.nseindia.com/archives/fo/cat/fo_cat_turnover_{trade_date.strftime(ddmmyy)}.xls"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        logger.error(
            f"Category turnover FO data not found for {trade_date.strftime('%d-%m-%Y')}"
        )
        raise NSEdataNotFound(f"No data available for : {trade_date}")

    raw = pd.read_excel(BytesIO(file_chk.content), header=None, engine="xlrd")
    header_row = None
    for index in range(min(len(raw), 16)):
        first = str(raw.iloc[index, 0]).strip()
        second = str(raw.iloc[index, 1]).strip() if raw.shape[1] > 1 else ""
        if first.lower() == "trade date" and second.lower() in {
            "category",
            "client categories",
        }:
            header_row = index
            break
    if header_row is None:
        logger.error(
            f"Could not locate header row in category turnover data for {trade_date.strftime('%d-%m-%Y')}"
        )
        raise NSEdataNotFound(f"Category turnover FO data not found for : {trade_date}")

    headers = [str(value).strip() for value in raw.iloc[header_row, :4].tolist()]
    end_row = header_row + 1
    while end_row < len(raw.index):
        first_value = raw.iloc[end_row, 0] if raw.shape[1] > 0 else None
        second_value = raw.iloc[end_row, 1] if raw.shape[1] > 1 else None
        first_text = str(first_value).strip() if first_value is not None else ""
        second_text = str(second_value).strip() if second_value is not None else ""
        if (
            first_value is None
            or (isinstance(first_value, float) and pd.isna(first_value))
            or first_text.lower().startswith("note")
            or first_text.lower().startswith("notes")
            or first_text.lower() == "trade date"
            or second_text.lower() in {"category", "client categories"}
        ):
            break
        end_row += 1

    data_df = raw.iloc[header_row + 1 : end_row, :4].copy()
    data_df.columns = headers
    data_df = data_df.rename(
        columns={
            "Trade Date": "Trade Date",
            "Category": "Category",
            "Client Categories": "Category",
            "Buy Value in Rs.Crores": "Buy Value in Rs.Crores",
            "Sell Value in Rs.Crores": "Sell Value in Rs.Crores",
        }
    )
    data_df = data_df[
        [
            column
            for column in [
                "Trade Date",
                "Category",
                "Buy Value in Rs.Crores",
                "Sell Value in Rs.Crores",
            ]
            if column in data_df.columns
        ]
    ].copy()
    data_df["Trade Date"] = pd.to_datetime(data_df["Trade Date"], errors="coerce")
    data_df["Category"] = data_df["Category"].astype(str).str.strip()
    data_df = data_df[data_df["Category"].ne("")].copy()
    for column_name in ["Buy Value in Rs.Crores", "Sell Value in Rs.Crores"]:
        data_df[column_name] = pd.to_numeric(data_df[column_name], errors="coerce")
    data_df = data_df.dropna(subset=["Trade Date", "Category"]).reset_index(drop=True)
    data_df["Net Value in Rs.Crores"] = (
        data_df["Buy Value in Rs.Crores"] - data_df["Sell Value in Rs.Crores"]
    )
    data_df["Trade Date"] = data_df["Trade Date"].dt.strftime("%d-%b-%Y")
    return data_df


def fii_derivatives_statistics(trade_date: str) -> pd.DataFrame:
    """
    Fetch specific FII derivatives statistics for a given trade date.

    Args:
        trade_date (str): The trade date in 'dd-mm-YYYY' format (e.g., '20-06-2023').

    Returns:
        pd.DataFrame: A DataFrame containing the FII derivatives statistics.

    Raises:
        NSEdataNotFound: If the statistics are not available for the given date.

    Example:
            from nselib import derivatives
            df = derivatives.fii_derivatives_statistics(trade_date='16-09-2024')
    """
    t_date = pd.to_datetime(trade_date, format="%d-%m-%Y")
    trade_date = t_date.strftime("%d-%b-%Y")
    logger.debug(f"Fetching FII derivatives statistics for trade date: {trade_date}")
    url = f"https://nsearchives.nseindia.com/content/fo/fii_stats_{trade_date}.xls"
    file_chk = nse_urlfetch(url)
    if file_chk.status_code != 200:
        logger.error(f"FII derivatives statistics not found for {trade_date}")
        raise NSEdataNotFound(f"No data available for : {trade_date}")
    try:
        bhav_df = pd.read_excel(
            BytesIO(file_chk.content), skiprows=3, skipfooter=10
        ).dropna()
        bhav_df.columns = [
            "fii_derivatives",
            "buy_contracts",
            "buy_value_in_Cr",
            "sell_contracts",
            "sell_value_in_Cr",
            "open_contracts",
            "open_contracts_value_in_Cr",
        ]
    except Exception as e:
        logger.error(
            f"Failed to parse FII derivatives statistics for {trade_date}",
            exc_info=e,
        )
        raise NSEdataNotFound(
            f"FII derivatives statistics not found for : {trade_date} :: NSE error : {e}"
        )
    return bhav_df


def expiry_dates_future() -> list:
    """
    Fetch the list of valid expiry dates for futures contracts.

    Returns:
        list: A list of expiration dates in 'dd-MMM-yyyy' format.

    Example:
            from nselib import derivatives
            exp_dates = derivatives.expiry_dates_future()
    """
    origin_url = "https://www.nseindia.com/option-chain"
    logger.debug("Fetching valid expiry dates for futures contracts.")
    payload = nse_urlfetch(
        "https://www.nseindia.com/api/option-chain-contract-info?symbol=TCS",
        origin_url=origin_url,
    ).json()
    return payload["expiryDates"]


def expiry_dates_option_index() -> dict:
    """
    Fetch the valid future and option expiry dates mapped to their underlying stock or index.

    Returns:
        dict: A dictionary mapping index/symbols to their exact list of expiry dates.

    Example:
            from nselib import derivatives
            index_dates_map = derivatives.expiry_dates_option_index()
    """
    # data_df = pd.DataFrame(columns=['index', 'expiry_date'])
    logger.debug("Fetching valid expiry dates for mapped option indices.")
    data_dict = {}
    for ind in indices_list:
        origin_url = "https://www.nseindia.com/option-chain"
        payload = nse_urlfetch(
            f"https://www.nseindia.com/api/option-chain-contract-info?symbol={ind}",
            origin_url=origin_url,
        ).json()
        data_dict.update({ind: payload["expiryDates"]})
    return data_dict


def nse_live_option_chain(
    symbol: str, expiry_date: str = None, oi_mode: str = "full"
) -> pd.DataFrame:
    """
    Fetch the live NSE option chain.

    Args:
        symbol (str): The NSE symbol (e.g., 'SBIN' or 'BANKNIFTY').
        expiry_date (str, optional): The specific expiry date to filter by, in 'dd-mm-YYYY' format.
        oi_mode (str, optional): The detail level of the Open Interest data. Can be 'full' or 'compact'.

    Returns:
        pd.DataFrame: A DataFrame containing the real-time option chain data.

    Example:
            from nselib import derivatives
            chain_df = derivatives.nse_live_option_chain(symbol='TCS', expiry_date='20-06-2023')
    """

    if expiry_date:
        exp_date = pd.to_datetime(expiry_date, format="%d-%m-%Y")
        expiry_date = pd.to_datetime(exp_date, format="%d-%m-%Y").strftime("%d-%b-%Y")
    logger.debug(
        f"Parsing live option chain data for symbol: {symbol}, expiry_date: {expiry_date}, mode: {oi_mode}"
    )
    payload = get_nse_option_chain(symbol, expiry_date).json()
    if oi_mode == "compact":
        col_names = [
            "Fetch_Time",
            "Symbol",
            "Expiry_Date",
            "CALLS_OI",
            "CALLS_Chng_in_OI",
            "CALLS_Volume",
            "CALLS_IV",
            "CALLS_LTP",
            "CALLS_Net_Chng",
            "Strike_Price",
            "PUTS_OI",
            "PUTS_Chng_in_OI",
            "PUTS_Volume",
            "PUTS_IV",
            "PUTS_LTP",
            "PUTS_Net_Chng",
        ]
    else:
        col_names = [
            "Fetch_Time",
            "Symbol",
            "Expiry_Date",
            "CALLS_OI",
            "CALLS_Chng_in_OI",
            "CALLS_Volume",
            "CALLS_IV",
            "CALLS_LTP",
            "CALLS_Net_Chng",
            "CALLS_Bid_Qty",
            "CALLS_Bid_Price",
            "CALLS_Ask_Price",
            "CALLS_Ask_Qty",
            "Strike_Price",
            "PUTS_Bid_Qty",
            "PUTS_Bid_Price",
            "PUTS_Ask_Price",
            "PUTS_Ask_Qty",
            "PUTS_Net_Chng",
            "PUTS_LTP",
            "PUTS_IV",
            "PUTS_Volume",
            "PUTS_Chng_in_OI",
            "PUTS_OI",
        ]

    oi_data = pd.DataFrame(columns=col_names)

    oi_row = {
        "Fetch_Time": None,
        "Symbol": None,
        "Expiry_Date": None,
        "CALLS_OI": 0,
        "CALLS_Chng_in_OI": 0,
        "CALLS_Volume": 0,
        "CALLS_IV": 0,
        "CALLS_LTP": 0,
        "CALLS_Net_Chng": 0,
        "CALLS_Bid_Qty": 0,
        "CALLS_Bid_Price": 0,
        "CALLS_Ask_Price": 0,
        "CALLS_Ask_Qty": 0,
        "Strike_Price": 0,
        "PUTS_OI": 0,
        "PUTS_Chng_in_OI": 0,
        "PUTS_Volume": 0,
        "PUTS_IV": 0,
        "PUTS_LTP": 0,
        "PUTS_Net_Chng": 0,
        "PUTS_Bid_Qty": 0,
        "PUTS_Bid_Price": 0,
        "PUTS_Ask_Price": 0,
        "PUTS_Ask_Qty": 0,
    }

    # print(expiry_date)
    for m in range(len(payload["records"]["data"])):
        if not expiry_date or (
            payload["records"]["data"][m]["expiryDates"] == expiry_date
        ):
            try:
                oi_row["Expiry_Date"] = payload["records"]["data"][m]["expiryDates"]
                oi_row["CALLS_OI"] = payload["records"]["data"][m]["CE"]["openInterest"]
                oi_row["CALLS_Chng_in_OI"] = payload["records"]["data"][m]["CE"][
                    "changeinOpenInterest"
                ]
                oi_row["CALLS_Volume"] = payload["records"]["data"][m]["CE"][
                    "totalTradedVolume"
                ]
                oi_row["CALLS_IV"] = payload["records"]["data"][m]["CE"][
                    "impliedVolatility"
                ]
                oi_row["CALLS_LTP"] = payload["records"]["data"][m]["CE"]["lastPrice"]
                oi_row["CALLS_Net_Chng"] = payload["records"]["data"][m]["CE"]["change"]
                if oi_mode == "full":
                    oi_row["CALLS_Bid_Qty"] = payload["records"]["data"][m]["CE"][
                        "buyQuantity1"
                    ]
                    oi_row["CALLS_Bid_Price"] = payload["records"]["data"][m]["CE"][
                        "buyPrice1"
                    ]
                    oi_row["CALLS_Ask_Price"] = payload["records"]["data"][m]["CE"][
                        "sellPrice1"
                    ]
                    oi_row["CALLS_Ask_Qty"] = payload["records"]["data"][m]["CE"][
                        "sellQuantity1"
                    ]
            except KeyError:
                (
                    oi_row["CALLS_OI"],
                    oi_row["CALLS_Chng_in_OI"],
                    oi_row["CALLS_Volume"],
                    oi_row["CALLS_IV"],
                    oi_row["CALLS_LTP"],
                    oi_row["CALLS_Net_Chng"],
                ) = 0, 0, 0, 0, 0, 0
                if oi_mode == "full":
                    (
                        oi_row["CALLS_Bid_Qty"],
                        oi_row["CALLS_Bid_Price"],
                        oi_row["CALLS_Ask_Price"],
                        oi_row["CALLS_Ask_Qty"],
                    ) = 0, 0, 0, 0
                pass

            oi_row["Strike_Price"] = payload["records"]["data"][m]["strikePrice"]

            try:
                oi_row["PUTS_OI"] = payload["records"]["data"][m]["PE"]["openInterest"]
                oi_row["PUTS_Chng_in_OI"] = payload["records"]["data"][m]["PE"][
                    "changeinOpenInterest"
                ]
                oi_row["PUTS_Volume"] = payload["records"]["data"][m]["PE"][
                    "totalTradedVolume"
                ]
                oi_row["PUTS_IV"] = payload["records"]["data"][m]["PE"][
                    "impliedVolatility"
                ]
                oi_row["PUTS_LTP"] = payload["records"]["data"][m]["PE"]["lastPrice"]
                oi_row["PUTS_Net_Chng"] = payload["records"]["data"][m]["PE"]["change"]
                if oi_mode == "full":
                    oi_row["PUTS_Bid_Qty"] = payload["records"]["data"][m]["PE"][
                        "buyQuantity1"
                    ]
                    oi_row["PUTS_Bid_Price"] = payload["records"]["data"][m]["PE"][
                        "buyPrice1"
                    ]
                    oi_row["PUTS_Ask_Price"] = payload["records"]["data"][m]["PE"][
                        "sellPrice1"
                    ]
                    oi_row["PUTS_Ask_Qty"] = payload["records"]["data"][m]["PE"][
                        "sellQuantity1"
                    ]
            except KeyError:
                (
                    oi_row["PUTS_OI"],
                    oi_row["PUTS_Chng_in_OI"],
                    oi_row["PUTS_Volume"],
                    oi_row["PUTS_IV"],
                    oi_row["PUTS_LTP"],
                    oi_row["PUTS_Net_Chng"],
                ) = 0, 0, 0, 0, 0, 0
                if oi_mode == "full":
                    (
                        oi_row["PUTS_Bid_Qty"],
                        oi_row["PUTS_Bid_Price"],
                        oi_row["PUTS_Ask_Price"],
                        oi_row["PUTS_Ask_Qty"],
                    ) = 0, 0, 0, 0

            # if oi_mode == 'full':
            #     oi_row['CALLS_Chart'], oi_row['PUTS_Chart'] = 0, 0
            if oi_data.empty:
                oi_data = pd.DataFrame([oi_row]).copy()
            else:
                oi_data = pd.concat(
                    [oi_data, pd.DataFrame([oi_row])], ignore_index=True
                )
            oi_data["Symbol"] = symbol
            oi_data["Fetch_Time"] = payload["records"]["timestamp"]
    return oi_data


def fno_security_in_ban_period(trade_date: str) -> list:
    """
    Fetch the list of securities which are banned from the F&O segment for a given trade date.

    Args:
        trade_date (str): The trade date in 'dd-mm-YYYY' format (e.g., '20-06-2023').

    Returns:
        list: A list of security symbols currently in the ban period.

    Example:
            from nselib import derivatives
            banned_securities = derivatives.fno_security_in_ban_period(trade_date='26-03-2025')
    """
    trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
    logger.debug(
        f"Fetching F&O securities in ban period for trade date: {trade_date.strftime('%d-%m-%Y')}"
    )
    url = "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_"
    payload = f"{str(trade_date.strftime('%d%m%Y'))}.csv"
    request = nse_urlfetch(url + payload)
    securities = []
    if request.status_code == 200:
        lines = request.content.decode("utf-8").strip().split("\n")
        securities = [line.split(",")[1] for line in lines[1:]]
    elif request.status_code == 403:
        url2 = (
            "https://www.nseindia.com/api/reports?archives="
            "%5B%7B%22name%22%3A%22F%26O%20-%20Security%20in%20ban%20period%22%2C%22type%22%3A%22archives%22%2C%22category%22"
            f"%3A%22derivatives%22%2C%22section%22%3A%22equity%22%7D%5D&date={str(trade_date.strftime('%d-%b-%Y'))}"
            f"&type=equity&mode=single"
        )
        request = nse_urlfetch(url2)
        if request.status_code == 200:
            lines = request.content.decode("utf-8").strip().split("\n")
            securities = [line.split(",")[1] for line in lines[1:]]
        elif request.status_code == 403:
            logger.error(
                f"F&O ban period data not found for {trade_date.strftime('%d-%m-%Y')}"
            )
            raise NSEdataNotFound("Data not found, change the date...")
    logger.debug(f"Successfully retrieved {len(securities)} securities in ban period.")
    return securities


def live_most_active_underlying() -> pd.DataFrame:
    """
    Fetch the most active underlyings in the live market.
    Note: After market hours, this will return data based on the last traded values.
    Source: https://www.nseindia.com/market-data/most-active-underlying

    Returns:
        pd.DataFrame: A DataFrame of the most active underlying assets.

    Raises:
        NSEdataNotFound: If the API resource is unavailable.

    Example:
            from nselib import derivatives
            df = derivatives.live_most_active_underlying()
    """
    origin_url = "https://www.nseindia.com/market-data/most-active-underlying"
    url = "https://www.nseindia.com/api/live-analysis-most-active-underlying"
    logger.debug("Fetching most active underlyings in live market.")
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
        data_df = pd.DataFrame(data_json["data"])
    except Exception as e:
        logger.error("Failed to fetch live most active underlyings", exc_info=e)
        raise NSEApiError(f"Resource not available MSG: {e}")
    logger.debug(
        f"Successfully retrieved {len(data_df)} records for most active underlyings."
    )
    return data_df


def _normalize_business_growth_fo_segment_financial_year(from_year, to_year):
    if to_year is None and from_year is not None and "-" in str(from_year):
        from_year, to_year = [part.strip() for part in str(from_year).split("-", 1)]
    if from_year is None or to_year is None:
        raise NSEdataNotFound(
            "For monthly data provide from_year and to_year, e.g. from_year='2025', to_year='2026'"
        )
    return str(from_year).strip(), str(to_year).strip()


def _normalize_business_growth_fo_segment_daily_args(month, year):
    if year is None and month is not None and "-" in str(month):
        month, year = [part.strip() for part in str(month).split("-", 1)]
    if month is None or year is None:
        raise NSEdataNotFound(
            "For daily data provide month and year, e.g. month='Mar', year='2026'"
        )

    month = str(month).strip()
    try:
        month = datetime.strptime(month[:3].title(), "%b").strftime("%b")
    except ValueError as exc:
        raise NSEdataNotFound(
            "Month should be a valid month name like 'Mar' or 'March'"
        ) from exc

    year = str(year).strip()
    if len(year) == 2 and year.isdigit():
        year = f"20{year}"
    return month, year


def _business_growth_fo_segment_dataframe(data_json):
    records = [row.get("data", row) for row in data_json.get("data", [])]
    data_df = pd.DataFrame(records)
    if data_df.empty:
        return data_df

    data_df = data_df.replace({r"\r": ""}, regex=True)
    preserve_columns = {"type", "TYPE", "date"}
    null_map = {
        "": np.nan,
        "-": np.nan,
        "--": np.nan,
        "None": np.nan,
        "nan": np.nan,
        "NaN": np.nan,
    }

    for column_name in data_df.columns:
        if column_name in preserve_columns:
            continue
        if pd.api.types.is_datetime64_any_dtype(data_df[column_name]):
            continue
        cleaned_series = (
            data_df[column_name]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.strip()
        )
        normalized_series = cleaned_series.replace(null_map)
        numeric_series = pd.to_numeric(normalized_series, errors="coerce")
        if numeric_series.notna().sum() == normalized_series.notna().sum():
            data_df[column_name] = numeric_series
        else:
            data_df[column_name] = normalized_series

    meta_keys = [key for key in data_json.keys() if key != "data"]
    if meta_keys:
        data_df.attrs["metadata"] = {key: data_json[key] for key in meta_keys}
    return data_df


def business_growth_fo_segment(
    data_type: str = "yearly",
    from_year: str = None,
    to_year: str = None,
    month: str = None,
    year: str = None,
):
    """
    Fetch historical business growth data for the NSE F&O segment.

    Args:
        data_type (str): The frequency of data to fetch. Must be one of {'yearly', 'monthly', 'daily'}. Defaults to 'yearly'.
        from_year (str): Required for monthly data. The starting year of the financial year (e.g., '2025' for FY 2025-2026).
        to_year (str): Required for monthly data. The ending year of the financial year (e.g., '2026' for FY 2025-2026).
        month (str): Required for daily data. The 3-letter abbreviated month name (e.g., 'Mar', 'March', or 'Mar-26').
        year (str): Required for daily data. The 4-digit or 2-digit year (e.g., '2026' or '26').

    Returns:
        pandas.DataFrame: A DataFrame containing the historical business growth F&O data.

    Raises:
        ValueError: If the required parameters for the specified `data_type` are missing or improperly formatted.

    Example:
            from nselib import derivatives
            df_yearly = derivatives.business_growth_fo_segment(data_type='yearly')
            df_monthly = derivatives.business_growth_fo_segment(data_type='monthly', from_year='2025', to_year='2026')
    """
    logger.debug(
        f"Fetching historical business growth data for F&O segment, frequency: {data_type}"
    )
    static_options_list = ["yearly", "monthly", "daily"]
    validate_param_from_list(data_type, static_options_list)

    if data_type == "yearly":
        data_json = get_business_growth_fo_segment_yearly()
    elif data_type == "monthly":
        from_year, to_year = _normalize_business_growth_fo_segment_financial_year(
            from_year, to_year
        )
        data_json = get_business_growth_fo_segment_monthly(
            from_year=from_year, to_year=to_year
        )
    else:
        month, year = _normalize_business_growth_fo_segment_daily_args(month, year)
        data_json = get_business_growth_fo_segment_daily(month=month, year=year)

    return _business_growth_fo_segment_dataframe(data_json)


if __name__ == '__main__':
    # df = future_price_volume_data("BANKNIFTY", "FUTIDX", from_date='01-11-2025', to_date='08-12-2025', period='6M')
    # df = option_price_volume_data('NIFTY', 'OPTIDX', period='1W')
    # df = nse_live_option_chain(symbol='TCS', expiry_date='02-01-2026')
    # df = fii_derivatives_statistics(trade_date='16-09-2024')
    # df = participant_wise_trading_volume(trade_date='16-09-2024')
    # df = fno_security_in_ban_period(trade_date='26-03-2025')
    # df = expiry_dates_option_index()
    # df = expiry_dates_future()
    # df = fno_bhav_copy('17-02-2025')
    df = live_most_active_underlying()
    # df = category_turnover_fo(trade_date='16-09-2025')
    # df = business_growth_fo_segment(data_type='yearly')
    # df = business_growth_fo_segment(data_type='monthly', from_year='2025', to_year='2026')
    # df = business_growth_fo_segment(data_type='daily', month='Mar', year='2026')
    print(df)
    # print(df.columns)
    # print(df[df['EXPIRY_DT']=='27-Jul-2023'])

```

#### `nselib/derivatives/get_func.py`

```python
import requests

from nselib.constants import indices_list
from nselib.errors import NSEdataNotFound
from nselib.libutil import *
from nselib.constants import future_price_volume_data_column

logger = logging.getLogger(__name__)


def get_future_price_volume_data(
    symbol: str, instrument: str, from_date: str, to_date: str
) -> pd.DataFrame:
    """
    Fetch historical price and volume data for a specific futures contract.

    Args:
        symbol (str): The ticker symbol of the underlying asset (e.g., 'SBIN', 'NIFTY').
        instrument (str): The type of futures instrument ('FUTIDX' for index futures, 'FUTSTK' for stock futures).
        from_date (str): The start date for the historical data in 'DD-MM-YYYY' format.
        to_date (str): The end date for the historical data in 'DD-MM-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the historical OHLCV data and other transaction details.

    Raises:
        ValueError: If the NSE API rejects the parameters or encounters an error during the fetch.

    Example:
            from nselib import derivatives
            df = derivatives.future_price_volume_data(symbol='SBIN', instrument='FUTSTK', period='1M')
    """
    logger.debug(
        f"Fetching future price volume data for symbol: {symbol}, instrument: {instrument}, from: {from_date}, to: {to_date}"
    )
    origin_url = "https://www.nseindia.com/report-detail/fo_eq_security"
    url = "https://www.nseindia.com/api/historicalOR/foCPV?"
    payload = f"from={from_date}&to={to_date}&instrumentType={instrument}&symbol={symbol}&csv=true"

    try:
        logger.debug("Making request to NSE API for future data.")
        data: dict = nse_urlfetch(url + payload, origin_url=origin_url).json()
    except Exception as e:
        logger.error(
            f"Failed to fetch future price volume data. Error: {e}", exc_info=e
        )
        raise ValueError(f" Invalid parameters : NSE error:{e}")

    data_df = pd.DataFrame(data["data"])
    data_df.columns = cleaning_column_name(data_df.columns)
    logger.debug(
        f"Successfully retrieved future price volume data with {len(data_df)} records."
    )
    return data_df


def get_option_price_volume_data(
    symbol: str, instrument: str, option_type: str, from_date: str, to_date: str
) -> pd.DataFrame:
    """
    Fetch historical price and volume data for a specific options contract.

    Args:
        symbol (str): The ticker symbol of the underlying asset (e.g., 'RELIANCE', 'BANKNIFTY').
        instrument (str): The type of options instrument ('OPTIDX' for index options, 'OPTSTK' for stock options).
        option_type (str): The type of option ('CE' for Call European, 'PE' for Put European).
        from_date (str): The start date for the historical data in 'DD-MM-YYYY' format.
        to_date (str): The end date for the historical data in 'DD-MM-YYYY' format.

    Returns:
        pandas.DataFrame: A DataFrame containing the historical OHLCV data for the requested option.

    Raises:
        ValueError: If the provided parameters yield no data, or if the NSE API throws an error.

    Example:
            from nselib import derivatives
            df = derivatives.option_price_volume_data(symbol='NIFTY', instrument='OPTIDX', option_type='CE', period='1M')
    """
    logger.debug(
        f"Fetching option price volume data for symbol: {symbol}, instrument: {instrument}, option type: {option_type}, from: {from_date}, to: {to_date}"
    )
    origin_url = "https://www.nseindia.com/report-detail/fo_eq_security"
    url = "https://www.nseindia.com/api/historicalOR/foCPV?"
    payload = (
        f"from={from_date}&to={to_date}&instrumentType={instrument}&symbol={symbol}"
        f"&optionType={option_type}&csv=true"
    )
    try:
        logger.debug("Making request to NSE API for option data.")
        data_dict = nse_urlfetch(url + payload, origin_url=origin_url).json()
    except Exception as e:
        logger.error(
            f"Failed to fetch option price volume data. Error: {e}", exc_info=e
        )
        raise ValueError(f" Invalid parameters : NSE error : {e}")

    data_df = pd.DataFrame(data_dict["data"])
    if data_df.empty:
        logger.warning(f"No option price volume data returned for symbol: {symbol}")
        raise ValueError("Invalid parameters, Please change the parameters")

    data_df.columns = cleaning_column_name(data_df.columns)
    logger.debug(
        f"Successfully retrieved option price volume data with {len(data_df)} records."
    )
    return data_df[future_price_volume_data_column]


def get_nse_option_chain(symbol: str, expiry_date: str):
    """
    Fetch the complete live option chain for a given symbol and expiry date.

    Args:
        symbol (str): The underlying symbol (e.g., 'TCS', 'NIFTY'). The function automatically determines if it's an index or equity.
        expiry_date (str): The exact expiration date in 'DD-MMM-YYYY' format (e.g., '25-Dec-2025').

    Returns:
        requests.Response: The HTTP response from the NSE API containing the option chain data payload.

    Example:
            from nselib import derivatives
            chain = derivatives.nse_live_option_chain(symbol='TCS', expiry_date='27-03-2026')
    """
    logger.debug(
        f"Fetching NSE option chain for symbol: {symbol}, expiry date: {expiry_date}"
    )
    symbol = cleaning_nse_symbol(symbol)
    origin_url = "https://www.nseindia.com/option-chain"

    if any(x in symbol for x in indices_list):
        logger.debug(f"Symbol '{symbol}' identified as an index.")
        url = f"https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry_date}"
    else:
        logger.debug(f"Symbol '{symbol}' identified as equity.")
        url = f"https://www.nseindia.com/api/option-chain-v3?type=Equity&symbol={symbol}&expiry={expiry_date}"

    logger.debug("Making request to NSE API for option chain data.")
    chain = nse_urlfetch(url, origin_url=origin_url)
    logger.debug("Successfully retrieved option chain data.")
    return chain


def _get_business_growth_fo_segment_data(api_path: str) -> dict:
    """
    Internal helper to fetch business growth data for the F&O segment from NSE.

    Establishes a fresh session with the NSE origin page to obtain cookies,
    then makes the actual API request.

    Args:
        api_path (str): The relative API path to query (e.g., '/api/historicalOR/fo/tbg/yearly').

    Returns:
        dict: The parsed JSON response from the NSE API.

    Raises:
        NSEdataNotFound: If the NSE API is unreachable or returns an error.
    """
    logger.debug(f"Fetching business growth F&O segment data from path: {api_path}")
    origin_url = "https://www.nseindia.com/market-data/business-growth-fo-segment"
    url = f"https://www.nseindia.com{api_path}"
    try:
        r_session = requests.session()
        r_session.trust_env = False
        nse_live = r_session.get(origin_url, headers=default_header)
        cookies = nse_live.cookies
        data_json = r_session.get(url, headers=header, cookies=cookies).json()
    except Exception as e:
        logger.error(
            f"Failed to fetch business growth F&O data. Error: {e}", exc_info=e
        )
        raise NSEdataNotFound(f" Resource not available MSG: {e}")
    logger.debug("Successfully retrieved business growth F&O segment data.")
    return data_json


def get_business_growth_fo_segment_yearly() -> dict:
    """
    Fetch yearly business growth data for the NSE F&O segment.

    Returns:
        dict: The JSON response containing yearly F&O business growth statistics.

    Example:
            from nselib import derivatives
            df = derivatives.business_growth_fo_segment(data_type='yearly')
    """
    logger.debug("Fetching yearly business growth F&O segment data.")
    return _get_business_growth_fo_segment_data("/api/historicalOR/fo/tbg/yearly")


def get_business_growth_fo_segment_monthly(from_year: str, to_year: str) -> dict:
    """
    Fetch monthly business growth data for the NSE F&O segment for a given financial year.

    Args:
        from_year (str): The starting year of the financial year (e.g., '2025').
        to_year (str): The ending year of the financial year (e.g., '2026').

    Returns:
        dict: The JSON response containing monthly F&O business growth statistics.

    Example:
            from nselib import derivatives
            df = derivatives.business_growth_fo_segment(data_type='monthly', from_year='2025', to_year='2026')
    """
    logger.debug(
        f"Fetching monthly business growth F&O segment data for FY {from_year}-{to_year}."
    )
    return _get_business_growth_fo_segment_data(
        f"/api/historicalOR/fo/tbg/monthly?from={from_year}&to={to_year}"
    )


def get_business_growth_fo_segment_daily(month: str, year: str) -> dict:
    """
    Fetch daily business growth data for the NSE F&O segment for a given month and year.

    Args:
        month (str): The 3-letter abbreviated month name (e.g., 'Mar').
        year (str): The full year (e.g., '2026').

    Returns:
        dict: The JSON response containing daily F&O business growth statistics.

    Example:
            from nselib import derivatives
            df = derivatives.business_growth_fo_segment(data_type='daily', month='Mar', year='2026')
    """
    logger.debug(f"Fetching daily business growth F&O segment data for {month} {year}.")
    return _get_business_growth_fo_segment_data(
        f"/api/historicalOR/fo/tbg/daily?month={month}&year={year}"
    )

```

#### `nselib/errors.py`

```python
from enum import Enum


class ErrorCodeEnum(Enum):
    """Error codes for NSElib"""

    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    API_ERROR = "API_ERROR"
    CALENDAR_NOT_FOUND = "CALENDAR_NOT_FOUND"
    DATA_NOT_FOUND = "DATA_NOT_FOUND"
    INVALID_INDEX_CATEGORY = "INVALID_INDEX_CATEGORY"
    INVALID_INDEX = "INVALID_INDEX"
    DERIVATIVE_INSTRUMENT_NOT_FOUND = "DERIVATIVE_INSTRUMENT_NOT_FOUND"


class NSEException(Exception):
    """Base class for all NSElib exceptions"""

    error_code: ErrorCodeEnum = ErrorCodeEnum.UNKNOWN_ERROR

    def __init__(
        self,
        message: str,
        error_code: ErrorCodeEnum = ErrorCodeEnum.UNKNOWN_ERROR,
    ):
        super().__init__(message)
        self.error_code: ErrorCodeEnum = error_code
        self.message: str = message


class NSEApiError(NSEException):
    """Exception raised when NSE API returns an error"""

    def __init__(self, message):
        super(NSEApiError, self).__init__(message, ErrorCodeEnum.API_ERROR)


class CalenderNotFound(NSEException):
    """Exception raised when a calendar is not found"""

    def __init__(self, message):
        super(CalenderNotFound, self).__init__(
            message, ErrorCodeEnum.CALENDAR_NOT_FOUND
        )


class DerivativeInstrumentNotFoundError(NSEException):
    """Exception raised when derivative instrument is not found"""

    def __init__(self, message):
        super(DerivativeInstrumentNotFoundError, self).__init__(
            message, ErrorCodeEnum.DERIVATIVE_INSTRUMENT_NOT_FOUND
        )


class NSEdataNotFound(NSEException):
    """Exception raised when data is not found"""

    def __init__(self, message):
        super(NSEdataNotFound, self).__init__(message, ErrorCodeEnum.DATA_NOT_FOUND)


class InvalidIndexCategoryError(NSEException):
    """Exception raised when category is not found"""

    def __init__(self, message):
        super(InvalidIndexCategoryError, self).__init__(
            message, ErrorCodeEnum.INVALID_INDEX_CATEGORY
        )


class InvalidIndexError(NSEException):
    """Exception raised when index is invalid"""

    def __init__(self, message):
        super(InvalidIndexError, self).__init__(message, ErrorCodeEnum.INVALID_INDEX)


class IndexDataNotFound(NSEException):
    """Exception raised when index data is not found"""

    def __init__(self, message):
        super(IndexDataNotFound, self).__init__(message, ErrorCodeEnum.DATA_NOT_FOUND)

```

#### `nselib/indices/__init__.py`

```python
from .index_data import index_list, constituent_stock_list, live_index_performances

```

#### `nselib/indices/index_data.py`

```python
from io import BytesIO
from nselib.indices import nse_config as conf
from nselib.libutil import nse_urlfetch
import pandas as pd
from nselib.errors import (
    InvalidIndexCategoryError,
    InvalidIndexError,
    IndexDataNotFound,
    NSEApiError,
)
import logging

logger = logging.getLogger(__name__)


def get_class(index_category: str) -> str:
    """
    Retrieve the configuration class for a given NSE index category.

    Args:
        index_category (str): The category of the index (e.g., 'BroadMarketIndices').

    Returns:
        class: The configuration class containing indices and URLs for the specified category.

    Raises:
        InvalidIndexCategoryError: If the provided index category does not exist.

    Example:
            from nselib import indices
            config_class = indices.index_data.get_class('BroadMarketIndices')
    """
    category = f"Nifty{index_category}"
    category_class = getattr(conf, category, None)

    if not category_class:
        raise InvalidIndexCategoryError(f"'{category}': is an invalid Index Category")

    return category_class


def index_list(index_category: str = "BroadMarketIndices") -> list:
    """
    Fetch the list of available NSE indices for a specific index category.

    The National Stock Exchange (NSE) classifies indices into four main categories:
    - SectoralIndices
    - BroadMarketIndices
    - ThematicIndices
    - StrategyIndices

    Reference: https://www.nseindia.com/static/products-services/about-indices

    Args:
        index_category (str): The category of indices to fetch. Defaults to 'BroadMarketIndices'.

    Returns:
        list: A list of index names belonging to the specified category.

    Example:
            from nselib import indices
            list_of_indices = indices.index_list('BroadMarketIndices')
    """
    return get_class(index_category).indices_list


def validate_index_category(index_category: str = "BroadMarketIndices") -> bool:
    """
    Validate if a given index category is supported by the NSE.

    Args:
        index_category (str): The category string to validate.

    Raises:
        InvalidIndexCategoryError: If the category is not one of the standardized NSE categories.

    Example:
            from nselib import indices
            is_valid = indices.index_data.validate_index_category('SectoralIndices')
    """
    category_list = [
        "SectoralIndices",
        "BroadMarketIndices",
        "ThematicIndices",
        "StrategyIndices",
    ]
    if index_category not in category_list:
        print(f"Valid Index Categories: {category_list}")
        raise InvalidIndexCategoryError(
            f"'{index_category}': is an invalid Index Category"
        )

    return True


def validate_index_name(
    index_category: str = "BroadMarketIndices", index_name: str = "Nifty 50"
) -> bool:
    """
    Validate if a given index name exists within a specified index category.

    Args:
        index_category (str): The category of the index. Defaults to 'BroadMarketIndices'.
        index_name (str): The name of the index to validate. Defaults to 'Nifty 50'.

    Raises:
        InvalidIndexError: If the index name is not found in the given category.

    Example:
            from nselib import indices
            is_valid = indices.index_data.validate_index_name('BroadMarketIndices', 'NIFTY 50')
    """
    validate_index_category(index_category)
    ind_list = index_list(index_category)
    if index_name not in ind_list:
        print(f"Valid Index Names: {ind_list}")
        raise InvalidIndexError(f"'{index_name}' is invalid index_name.")

    return True


def constituent_stock_list(
    index_category: str = "BroadMarketIndices", index_name: str = "Nifty 50"
) -> pd.DataFrame:
    """
    Retrieve the list of constituent stocks for a specific NSE index.

    Fetches the current composition of the requested index as a pandas DataFrame.
    It also prints a link to the index's official fact-sheet for more details.

    Args:
        index_category (str): The category of the index. Defaults to 'BroadMarketIndices'.
        index_name (str): The specific name of the index. Defaults to 'Nifty 50'.

    Returns:
        pandas.DataFrame: A DataFrame containing the constituent stocks and their details.

    Raises:
        InvalidIndexError: If the index category or name is invalid.
        IndexDataNotFound: If the data for the index is unavailable or fails to download.

    Example:
            from nselib import indices
            df = indices.constituent_stock_list('BroadMarketIndices', 'NIFTY 50')
    """
    logger.debug(f"Fetching constituent stock list for index_name: {index_name} in category: {index_category}")
    validate_index_name(index_category, index_name)
    url = get_class(index_category).index_constituent_list_urls[index_name]
    if not url:
        raise IndexDataNotFound(f"'{index_name}': No Data found for index")

    response = nse_urlfetch(url)
    if response.status_code == 200:
        stocks_df = pd.read_csv(BytesIO(response.content))
    else:
        raise IndexDataNotFound(
            f"'{index_name}': No Data found for index, Kindly check the "
            "index category & name"
        )

    url_fs = get_class(index_category).index_factsheet_urls[index_name]
    print(
        f" Note: For more detail information related to {index_name}. "
        f"Please check the Fact Sheet - {url_fs}"
    )
    return stocks_df


def live_index_performances() -> pd.DataFrame:
    """
    Fetch the live or last traded performance data for all NSE indices.

    During market hours, this returns live data. After market hours, it returns
    the final index performance data for the day.

    Data Source: https://www.nseindia.com/market-data/index-performances

    Returns:
        pandas.DataFrame: A DataFrame containing performance metrics for all indices.

    Raises:
        NSEApiError: If the NSE API is unreachable or returns an error.

    Example:
            from nselib import indices
            df = indices.live_index_performances()
    """
    logger.debug("Fetching live index performances for all indices")
    origin_url = "https://www.nseindia.com/market-data/index-performances"
    url = f"https://www.nseindia.com/api/allIndices"
    try:
        data_json = nse_urlfetch(url, origin_url=origin_url).json()
        data_df = pd.DataFrame(data_json["data"])
    except Exception as e:
        print(f"NSE Resource not available: {e}")
        raise NSEApiError(f" Resource not available MSG: {e}")

    data_df.drop(
        columns=["chartTodayPath", "chart30dPath", "chart365dPath"], inplace=True
    )
    return data_df


# if __name__ == '__main__':

# data = constituent_stock_list(index_category='BroadMarketIndices', index_name='Nifty 50')
# data = live_index_performances()
# print(data)

```

#### `nselib/indices/nse_config.py`

```python
class NiftySectoralIndices:
    indices_list = ["Nifty Auto", "Nifty Bank", "Nifty Chemicals", "Nifty Financial Services", "Nifty FMCG",
                    "Nifty Healthcare", "Nifty IT", "Nifty Media", "Nifty Metal", "Nifty Pharma", "Nifty Private Bank",
                    "Nifty PSU Bank", "Nifty Realty", "Nifty Consumer Durables", "Nifty Oil and Gas",
                    "Nifty500 Healthcare", "Nifty MidSmall Financial Services", "Nifty MidSmall Healthcare",
                    "Nifty MidSmall IT & Telecom"]
    index_constituent_list_urls = {
        "Nifty Auto": "https://www.niftyindices.com/IndexConstituent/ind_niftyautolist.csv",
        "Nifty Bank": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
        "Nifty Chemicals": "https://niftyindices.com/IndexConstituent/ind_niftyChemicals_list.csv",
        "Nifty Financial Services": "https://www.niftyindices.com/IndexConstituent/ind_niftyfinancelist.csv",
        "Nifty FMCG": "https://www.niftyindices.com/IndexConstituent/ind_niftyfmcglist.csv",
        "Nifty Healthcare": "https://www.niftyindices.com/IndexConstituent/ind_niftyhealthcarelist.csv",
        "Nifty IT": "https://www.niftyindices.com/IndexConstituent/ind_niftyitlist.csv",
        "Nifty Media": "https://www.niftyindices.com/IndexConstituent/ind_niftymedialist.csv",
        "Nifty Metal": "https://www.niftyindices.com/IndexConstituent/ind_niftymetallist.csv",
        "Nifty Pharma": "https://www.niftyindices.com/IndexConstituent/ind_niftypharmalist.csv",
        "Nifty Private Bank": "https://www.niftyindices.com/IndexConstituent/ind_nifty_privatebanklist.csv",
        "Nifty PSU Bank": "https://www.niftyindices.com/IndexConstituent/ind_niftypsubanklist.csv",
        "Nifty Realty": "https://www.niftyindices.com/IndexConstituent/ind_niftyrealtylist.csv",
        "Nifty Consumer Durables": "https://www.niftyindices.com/IndexConstituent/ind_niftyconsumerdurableslist.csv",
        "Nifty Oil and Gas": "https://www.niftyindices.com/IndexConstituent/ind_niftyoilgaslist.csv",
        "Nifty500 Healthcare": "https://niftyindices.com/IndexConstituent/ind_nifty500Healthcare_list.csv",
        "Nifty MidSmall Financial Services": "https://www.niftyindices.com/IndexConstituent/ind_niftymidsmallfinancailservice_list.csv",
        "Nifty MidSmall Healthcare": "https://www.niftyindices.com/IndexConstituent/ind_niftymidsmallhealthcare_list.csv",
        "Nifty MidSmall IT & Telecom": "https://www.niftyindices.com/IndexConstituent/ind_niftymidsmallitAndtelecom_list.csv"
    }
    index_factsheet_urls = {
        "Nifty Auto": "https://www.niftyindices.com/Factsheet/ind_nifty_auto.pdf",
        "Nifty Bank": "https://www.niftyindices.com/Factsheet/ind_nifty_bank.pdf",
        "Nifty Chemicals": "https://niftyindices.com/Factsheet/Factsheet_Nifty_Chemicals.pdf",
        "Nifty Financial Services": "https://www.niftyindices.com/Factsheet/ind_Nifty_Financial_Services.pdf",
        "Nifty FMCG": "https://www.niftyindices.com/Factsheet/ind_nifty_FMCG.pdf",
        "Nifty Healthcare": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty_Healthcare_Index.pdf",
        "Nifty IT": "https://www.niftyindices.com/Factsheet/ind_nifty_it.pdf",
        "Nifty Media": "https://www.niftyindices.com/Factsheet/ind_nifty_media.pdf",
        "Nifty Metal": "https://www.niftyindices.com/Factsheet/ind_nifty_metal.pdf",
        "Nifty Pharma": "https://www.niftyindices.com/Factsheet/ind_nifty_pharma.pdf",
        "Nifty Private Bank": "https://www.niftyindices.com/Factsheet/ind_nifty_private_bank.pdf",
        "Nifty PSU Bank": "https://www.niftyindices.com/Factsheet/ind_nifty_psu_bank.pdf",
        "Nifty Realty": "https://www.niftyindices.com/Factsheet/ind_nifty_realty.pdf",
        "Nifty Consumer Durables": "https://www.niftyindices.com/Factsheet/Factsheet_nifty_consumer_durables.pdf",
        "Nifty Oil and Gas": "https://www.niftyindices.com/Factsheet/Factsheet_nifty_oil_and_gas.pdf",
        "Nifty500 Healthcare": "https://niftyindices.com/Factsheet/Factsheet_Nifty500Healthcare.pdf",
        "Nifty MidSmall Financial Services": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyMidSmallFinancialSevices.pdf",
        "Nifty MidSmall Healthcare": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyMidSmallHealthCare.pdf",
        "Nifty MidSmall IT & Telecom": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyMidSmallITAndTelecom.pdf"
    }


class NiftyBroadMarketIndices:
    indices_list = ["Nifty 50", "Nifty Next 50", "Nifty 100", "Nifty 200", "Nifty Total Market", "Nifty 500",
                    "Nifty Midcap 150", "Nifty Midcap 50", "Nifty Midcap Select", "Nifty Midcap 100",
                    "Nifty Smallcap 250", "Nifty Smallcap 50", "Nifty Smallcap 100", "Nifty Microcap 250",
                    "Nifty LargeMidcap 250", "Nifty MidSmallcap 400", "Nifty India FPI 150", "India Vix"]
    index_constituent_list_urls = {
        "Nifty 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
        "Nifty Next 50": "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv",
        "Nifty 100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
        "Nifty 200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
        "Nifty Total Market": "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
        "Nifty 500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        "Nifty Midcap 150": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
        "Nifty Midcap 50": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap50list.csv",
        "Nifty Midcap Select": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcapselect_list.csv",
        "Nifty Midcap 100": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap100list.csv",
        "Nifty Smallcap 250": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
        "Nifty Smallcap 50": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap50list.csv",
        "Nifty Smallcap 100": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
        "Nifty Microcap 250": "https://nsearchives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
        "Nifty LargeMidcap 250": "https://nsearchives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv",
        "Nifty MidSmallcap 400": "https://nsearchives.nseindia.com/content/indices/ind_niftymidsmallcap400list.csv",
        "Nifty India FPI 150": "https://niftyindices.com/IndexConstituent/ind_niftyIndiaFPI150_list.csv"
    }
    index_factsheet_urls = {
        "Nifty 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50.pdf",
        "Nifty Next 50": "https://nsearchives.nseindia.com/content/indices/ind_next50.pdf",
        "Nifty 100": "https://nsearchives.nseindia.com/content/indices/ind_nifty_100.pdf",
        "Nifty 200": "https://nsearchives.nseindia.com/content/indices/ind_nifty_200.pdf",
        "Nifty Total Market": "https://nsearchives.nseindia.com/content/indices/Factsheet_NiftyTotalMarket.pdf",
        "Nifty 500": "https://nsearchives.nseindia.com/content/indices/ind_nifty_500.pdf",
        "Nifty Midcap 150": "https://nsearchives.nseindia.com/content/indices/ind_Nifty_Midcap_150.pdf",
        "Nifty Midcap 50": "https://nsearchives.nseindia.com/content/indices/ind_nifty_midcap50.pdf",
        "Nifty Midcap Select": "https://nsearchives.nseindia.com/content/indices/Factsheet_NiftyMidcapSelect.pdf",
        "Nifty Midcap 100": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap100.pdf",
        "Nifty Smallcap 250": "https://nsearchives.nseindia.com/content/indices/ind_Nifty_Smallcap_250.pdf",
        "Nifty Smallcap 50": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap_50.pdf",
        "Nifty Smallcap 100": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap100.pdf",
        "Nifty Microcap 250": "https://nsearchives.nseindia.com/content/indices/Factsheet_Nifty_Microcap_250_Index.pdf",
        "Nifty LargeMidcap 250": "https://nsearchives.nseindia.com/content/indices/Factsheet_NIFTY_LargeMidcap_250_Index.pdf",
        "Nifty MidSmallcap 400": "https://nsearchives.nseindia.com/content/indices/ind_Nifty_MidSmallcap_400.pdf",
        "Nifty India FPI 150": "https://niftyindices.com/Factsheet/Factsheet_NiftyIndiaFPI150Index.pdf"
    }


class NiftyThematicIndices:
    indices_list = [
        "Nifty Capital Markets",
        "Nifty Commodities",
        "Nifty Conglomerate 50",
        "Nifty Core Housing",
        "Nifty CPSE",
        "Nifty EV & New Age Automotive",
        "Nifty Energy",
        "Nifty Housing",
        "Nifty India Consumption",
        "Nifty India Defence",
        "Nifty India Digital",
        "Nifty India Infrastructure & Logistics",
        "Nifty India Internet",
        "Nifty India Manufacturing",
        "Nifty India New Age Consumption",
        "Nifty India Railways PSU",
        "Nifty India Tourism",
        "Nifty Infrastructure",
        "Nifty IPO",
        "Nifty Midcap Liquid 15",
        "Nifty MNC",
        "Nifty Mobility",
        "Nifty PSE",
        "Nifty REITs & InvITs",
        "Nifty Rural",
        "Nifty Non-Cyclical Consumer",
        "Nifty Services Sector",
        "Nifty Shariah 25",
        "Nifty100 Liquid 15",
        "Nifty50 Shariah",
        "Nifty500 Shariah",
        "Nifty SME EMERGE",
        "Nifty Waves",
        "Nifty100 ESG",
        "Nifty100 Enhanced ESG",
        "Nifty100 ESG Sector Leaders",
        "Nifty India Corporate Group - Aditya Birla Group",
        "Nifty India Corporate Group - Mahindra Group",
        "Nifty India Corporate Group - Tata Group",
        "Nifty India Select 5 Corporate Groups (MAATR)"
    ]
    index_constituent_list_urls = {
        "Nifty Capital Markets": "https://niftyindices.com/IndexConstituent/ind_niftyCapitalMarkets_list.csv",
        "Nifty Commodities": "https://www.niftyindices.com/IndexConstituent/ind_niftycommoditieslist.csv",
        "Nifty Conglomerate 50": "https://www.niftyindices.com/IndexConstituent/ind_niftyConglomerate50_list.csv",
        "Nifty Core Housing": "https://niftyindices.com/IndexConstituent/ind_niftyCoreHousing_list.csv",
        "Nifty CPSE": "https://www.niftyindices.com/IndexConstituent/ind_niftycpselist.csv",
        "Nifty EV & New Age Automotive": "https://niftyindices.com/IndexConstituent/ind_niftyEv_NewAgeAutomotive_list.csv",
        "Nifty Energy": "https://www.niftyindices.com/IndexConstituent/ind_niftyenergylist.csv",
        "Nifty Housing": "https://www.niftyindices.com/IndexConstituent/ind_niftyhousing_list.csv",
        "Nifty India Consumption": "https://www.niftyindices.com/IndexConstituent/ind_niftyconsumptionlist.csv",
        "Nifty India Defence": "https://www.niftyindices.com/IndexConstituent/ind_niftyindiadefence_list.csv",
        "Nifty India Digital": "https://niftyindices.com/IndexConstituent/ind_niftyindiadigital_list.csv",
        "Nifty India Infrastructure & Logistics": "https://niftyindices.com/IndexConstituent/ind_niftyIndiaInfrastructure_Logistics_list.csv",
        "Nifty India Internet": "https://www.niftyindices.com/IndexConstituent/ind_niftyIndiaInternet_list.csv",
        "Nifty India Manufacturing": "https://www.niftyindices.com/IndexConstituent/ind_niftyindiamanufacturing_list.csv",
        "Nifty India New Age Consumption": "https://niftyindices.com/IndexConstituent/ind_niftyIndiaNewAgeConsumption_list.csv",
        "Nifty India Railways PSU": "https://niftyindices.com/IndexConstituent/ind_niftyIndiaRailwaysPSU_list.csv",
        "Nifty India Tourism": "https://niftyindices.com/IndexConstituent/ind_niftyindiatourism_list.csv",
        "Nifty Infrastructure": "https://www.niftyindices.com/IndexConstituent/ind_niftyinfralist.csv",
        "Nifty IPO": "https://niftyindices.com/IndexConstituent/ind_niftyIPO_list.csv",
        "Nifty Midcap Liquid 15": "https://www.niftyindices.com/IndexConstituent/ind_Nifty_Midcap_Liquid15.csv",
        "Nifty MNC": "https://www.niftyindices.com/IndexConstituent/ind_niftymnclist.csv",
        "Nifty Mobility": "https://www.niftyindices.com/IndexConstituent/ind_niftymobility_list.csv",
        "Nifty PSE": "https://www.niftyindices.com/IndexConstituent/ind_niftypselist.csv",
        "Nifty REITs & InvITs": "https://www.niftyindices.com/IndexConstituent/ind_niftyREITs_InvITs_list.csv",
        "Nifty Rural": "https://niftyindices.com/IndexConstituent/ind_niftyRural_list.csv",
        "Nifty Non-Cyclical Consumer": "https://www.niftyindices.com/IndexConstituent/ind_niftynon-cyclicalconsumer_list.csv",
        "Nifty Services Sector": "https://www.niftyindices.com/IndexConstituent/ind_niftyservicelist.csv",
        "Nifty Shariah 25": "",
        "Nifty100 Liquid 15": "https://www.niftyindices.com/IndexConstituent/ind_Nifty100_Liquid15.csv",
        "Nifty50 Shariah": "",
        "Nifty500 Shariah": "",
        "Nifty SME EMERGE": "https://www.niftyindices.com/IndexConstituent/ind_niftysmelist.csv",
        "Nifty Waves": "https://niftyindices.com/IndexConstituent/ind_niftyWaves_list.csv",
        "Nifty100 ESG": "",
        "Nifty100 Enhanced ESG": "",
        "Nifty100 ESG Sector Leaders": "",
        "Nifty India Corporate Group - Aditya Birla Group": "https://niftyindices.com/IndexConstituent/ind_nifty_adityabirlalist.csv",
        "Nifty India Corporate Group - Mahindra Group": "https://niftyindices.com/IndexConstituent/ind_nifty_mahindralist.csv",
        "Nifty India Corporate Group - Tata Group": "https://niftyindices.com/IndexConstituent/ind_nifty_tatalist.csv",
        "Nifty India Select 5 Corporate Groups (MAATR)": "https://niftyindices.com/IndexConstituent/ind_niftyIndiaSelect5CorporateGroupsMAATR_list.csv"
    }
    index_factsheet_urls = {
        "Nifty Capital Markets": "https://niftyindices.com/Factsheet/Factsheet_NiftyCapitalMarkets.pdf",
        "Nifty Commodities": "https://www.niftyindices.com/Factsheet/ind_nifty_commodities.pdf",
        "Nifty Conglomerate 50": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyConglomerate50.pdf",
        "Nifty Core Housing": "https://niftyindices.com/Factsheet/Nifty_Core_Housing_Factsheet.pdf",
        "Nifty CPSE": "https://www.niftyindices.com/Factsheet/ind_Nifty_CPSE.pdf",
        "Nifty EV & New Age Automotive": "https://niftyindices.com/Factsheet/Factsheet_NiftyEVNewAgeAutomotive.pdf",
        "Nifty Energy": "https://www.niftyindices.com/Factsheet/ind_nifty_energy.pdf",
        "Nifty Housing": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyHousing.pdf",
        "Nifty India Consumption": "https://www.niftyindices.com/Factsheet/ind_Nifty_India_Consumption.pdf",
        "Nifty India Defence": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyIndiaDefence.pdf",
        "Nifty India Digital": "https://niftyindices.com/Factsheet/Factsheet_NiftyIndiaDigital.pdf",
        "Nifty India Infrastructure & Logistics": "https://niftyindices.com/Factsheet/Factsheet_Nifty_India_Infrastructure_and_Logistics.pdf",
        "Nifty India Internet": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty_India_Internet.pdf",
        "Nifty India Manufacturing": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty_India_Manufacturing_Index.pdf",
        "Nifty India New Age Consumption": "https://niftyindices.com/Factsheet/Factsheet_NiftyIndiaNewAgeConsumption.pdf",
        "Nifty India Railways PSU": "https://niftyindices.com/Factsheet/Factsheet_NiftyIndiaRailwaysPSU.pdf",
        "Nifty India Tourism": "https://niftyindices.com/Factsheet/Factsheet_NiftyIndiaTourism.pdf",
        "Nifty Infrastructure": "https://www.niftyindices.com/Factsheet/ind_nifty_infra.pdf",
        "Nifty IPO": "https://niftyindices.com/Factsheet/Factsheet_NiftyIPO.pdf",
        "Nifty Midcap Liquid 15": "https://www.niftyindices.com/Factsheet/Nifty_midcap_liquid_15.pdf",
        "Nifty MNC": "https://www.niftyindices.com/Factsheet/ind_nifty_mnc.pdf",
        "Nifty Mobility": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty_Mobility_Total_Returns_Index.pdf",
        "Nifty PSE": "https://www.niftyindices.com/Factsheet/ind_nifty_pse.pdf",
        "Nifty REITs & InvITs": "https://www.niftyindices.com/Factsheet/Factsheet_REITs_InvITs.pdf",
        "Nifty Rural": "https://niftyindices.com/Factsheet/Factsheet_NiftyRural.pdf",
        "Nifty Non-Cyclical Consumer": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyNonCyclicalConsumer.pdf",
        "Nifty Services Sector": "https://www.niftyindices.com/Factsheet/ind_nifty_services_sector.pdf",
        "Nifty Shariah 25": "https://www.niftyindices.com/Factsheet/Factsheet_nifty_Shariah25.pdf",
        "Nifty100 Liquid 15": "https://www.niftyindices.com/Factsheet/Nifty100_Liquid_15.pdf",
        "Nifty50 Shariah": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty50_Shariah.pdf",
        "Nifty500 Shariah": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty500_Shariah.pdf",
        "Nifty SME EMERGE": "https://www.niftyindices.com/Factsheet/Factsheet_NIFTY_SME_EMERGE_Index.pdf",
        "Nifty Waves": "https://niftyindices.com/Factsheet/Factsheet_Nifty_Waves.pdf",
        "Nifty100 ESG": "https://www.niftyindices.com/Factsheet/Factsheet_NIFTY100_ESG_Index.pdf",
        "Nifty100 Enhanced ESG": "https://www.niftyindices.com/Factsheet/Factsheet_NIFTY100_Enhanced_ESG_Index.pdf",
        "Nifty100 ESG Sector Leaders": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty100_ESG_Sector_Leaders.pdf",
        "Nifty India Corporate Group - Aditya Birla Group": "https://niftyindices.com/Factsheet/ind_NIFTY_ADITYA_BIRLA_GROUP.pdf",
        "Nifty India Corporate Group - Mahindra Group": "https://niftyindices.com/Factsheet/ind_NIFTY_MAHINDRA_GROUP.pdf",
        "Nifty India Corporate Group - Tata Group": "https://niftyindices.com/Factsheet/Ind_NIFTY_TATA_GROUP.pdf",
        "Nifty India Select 5 Corporate Groups (MAATR)": "https://niftyindices.com/Factsheet/Factsheet_NiftyIndiaSelect5CorporateGroupsMAATR.pdf"
    }


class NiftyStrategyIndices:
    indices_list = [
        "Nifty100 Equal Weight",
        "Nifty100 Low Volatility 30",
        "Nifty200 Momentum 30",
        "Nifty200 Alpha 30",
        "Nifty100 Alpha 30",
        "Nifty Dividend Opportunities 50",
        "Nifty Growth Sectors 15",
        "Nifty High Beta 50",
        "Nifty Low Volatility 50",
        "Nifty Top 10 Equal Weight",
        "Nifty Top 15 Equal Weight",
        "Nifty Top 20 Equal Weight",
        "Nifty100 Quality 30",
        "Nifty Midcap150 Momentum 50",
        "Nifty Midcap150 Quality 50",
        "Nifty Alpha 50",
        "Nifty50 Equal Weight",
        "Nifty50 Value 20",
        "Nifty200 Value 30",
        "Nifty500 Flexicap Quality 30",
        "Nifty500 Value 50",
        "Nifty500 Equal Weight",
        "Nifty500 Low Volatility 50",
        "Nifty500 Momentum 50",
        "Nifty500 Quality 50",
        "Nifty500 Multifactor MQVLv 50",
        "Nifty200 Quality 30",
        "Nifty MidSmallcap400 Momentum Quality 100",
        "Nifty Smallcap250 Quality 50",
        "Nifty Total Market Momentum Quality 50"
    ]
    index_constituent_list_urls = {
        "Nifty100 Equal Weight": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
        "Nifty100 Low Volatility 30": "https://www.niftyindices.com/IndexConstituent/ind_Nifty100LowVolatility30list.csv",
        "Nifty200 Momentum 30": "https://www.niftyindices.com/IndexConstituent/ind_nifty200Momentum30_list.csv",
        "Nifty200 Alpha 30": "https://www.niftyindices.com/IndexConstituent/ind_nifty200alpha30_list.csv",
        "Nifty100 Alpha 30": "https://www.niftyindices.com/IndexConstituent/ind_nifty100Alpha30list.csv",
        "Nifty Dividend Opportunities 50": "https://www.niftyindices.com/IndexConstituent/ind_niftydivopp50list.csv",
        "Nifty Growth Sectors 15": "https://www.niftyindices.com/IndexConstituent/ind_NiftyGrowth_Sectors15_Index.csv",
        "Nifty High Beta 50": "https://www.niftyindices.com/IndexConstituent/nifty_High_Beta50_Index.csv",
        "Nifty Low Volatility 50": "https://www.niftyindices.com/IndexConstituent/nifty_low_Volatility50_Index.csv",
        "Nifty Top 10 Equal Weight": "https://niftyindices.com/IndexConstituent/ind_niftytop10EqualWeight_list.csv",
        "Nifty Top 15 Equal Weight": "https://niftyindices.com/IndexConstituent/ind_niftytop15EqualWeight_list.csv",
        "Nifty Top 20 Equal Weight": "https://niftyindices.com/IndexConstituent/ind_niftyTop20EqualWeight_list.csv",
        "Nifty100 Quality 30": "https://www.niftyindices.com/IndexConstituent/ind_nifty100Quality30list.csv",
        "Nifty Midcap150 Momentum 50": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150momentum50_list.csv",
        "Nifty Midcap150 Quality 50": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150quality50list.csv",
        "Nifty Alpha 50": "https://www.niftyindices.com/IndexConstituent/ind_nifty_Alpha_Index.csv",
        "Nifty50 Equal Weight": "https://www.niftyindices.com/IndexConstituent/ind_Nifty50EqualWeight.csv",
        "Nifty50 Value 20": "https://www.niftyindices.com/IndexConstituent/ind_Nifty50_Value20.csv",
        "Nifty200 Value 30": "https://niftyindices.com/IndexConstituent/ind_nifty200Value30_list.csv",
        "Nifty500 Flexicap Quality 30": "https://niftyindices.com/IndexConstituent/ind_nifty500FlexicapQuality30_list.csv",
        "Nifty500 Value 50": "https://www.niftyindices.com/IndexConstituent/ind_nifty500Value50_list.csv",
        "Nifty500 Equal Weight": "https://niftyindices.com/IndexConstituent/ind_nifty500EqualWeight_list.csv",
        "Nifty500 Low Volatility 50": "https://niftyindices.com/IndexConstituent/ind_nifty500LowVolatility50_list.csv",
        "Nifty500 Momentum 50": "https://www.niftyindices.com/IndexConstituent/ind_nifty500Momentum50_list.csv",
        "Nifty500 Quality 50": "https://niftyindices.com/IndexConstituent/ind_nifty500Quality50_list.csv",
        "Nifty500 Multifactor MQVLv 50": "https://niftyindices.com/IndexConstituent/ind_nifty500MultifactorMQVLv50_list.csv",
        "Nifty200 Quality 30": "https://www.niftyindices.com/IndexConstituent/ind_nifty200Quality30_list.csv",
        "Nifty MidSmallcap400 Momentum Quality 100": "https://www.niftyindices.com/IndexConstituent/ind_niftyMidSmallcap400MomentumQuality100_list.csv",
        "Nifty Smallcap250 Quality 50": "https://www.niftyindices.com/IndexConstituent/ind_niftySmallcap250_Quality50_list.csv",
        "Nifty Total Market Momentum Quality 50": "https://niftyindices.com/IndexConstituent/ind_niftyTotalMarketMomentumQuality50_list.csv"
    }
    index_factsheet_urls = {
        "Nifty100 Equal Weight": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty_100_EW_Index.pdf",
        "Nifty100 Low Volatility 30": "https://www.niftyindices.com/Factsheet/Nifty100_LowVolatility30.pdf",
        "Nifty200 Momentum 30": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty200_Momentum30.pdf",
        "Nifty200 Alpha 30": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty200Alpha30.pdf",
        "Nifty100 Alpha 30": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty100_Alpha30.pdf",
        "Nifty Dividend Opportunities 50": "https://www.niftyindices.com/Factsheet/ind_Nifty_Divid_Opp50.pdf",
        "Nifty Growth Sectors 15": "https://www.niftyindices.com/Factsheet/Nifty_Growth_Sectors15.pdf",
        "Nifty High Beta 50": "https://www.niftyindices.com/Factsheet/Factsheet_nifty_High_Beta50.pdf",
        "Nifty Low Volatility 50": "https://www.niftyindices.com/Factsheet/Factsheet_nifty_Low_Volatility50.pdf",
        "Nifty Top 10 Equal Weight": "https://niftyindices.com/Factsheet/Factsheet_NiftyTop10EqualWeight.pdf",
        "Nifty Top 15 Equal Weight": "https://niftyindices.com/Factsheet/Factsheet_NiftyTop15EqualWeight.pdf",
        "Nifty Top 20 Equal Weight": "https://niftyindices.com/Factsheet/Factsheet_NiftyTop20EqualWeight.pdf",
        "Nifty100 Quality 30": "https://www.niftyindices.com/Factsheet/Nifty100_Quality30.pdf",
        "Nifty Midcap150 Momentum 50": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyMidcap150Momentum50.pdf",
        "Nifty Midcap150 Quality 50": "https://www.niftyindices.com/Factsheet/Factsheet_NIFTYMidcap150_Quality50.pdf",
        "Nifty Alpha 50": "https://www.niftyindices.com/Factsheet/Factsheet_Nifty_Alpha50.pdf",
        "Nifty50 Equal Weight": "https://www.niftyindices.com/Factsheet/Factsheet_NIFTY50_Equal_Weight.pdf",
        "Nifty50 Value 20": "https://www.niftyindices.com/Factsheet/Nifty50_Value20.pdf",
        "Nifty200 Value 30": "https://niftyindices.com/Factsheet/Factsheet_Nifty200_Value30.pdf",
        "Nifty500 Flexicap Quality 30": "https://niftyindices.com/Factsheet/Factsheet_Nifty500_Flexicap_Quality_30.pdf",
        "Nifty500 Value 50": "https://www.niftyindices.com/Factsheet/FactsheetNIFTY500Value50.pdf",
        "Nifty500 Equal Weight": "https://niftyindices.com/Factsheet/Factsheet_Nifty_500_Equal_Weight.pdf",
        "Nifty500 Low Volatility 50": "https://niftyindices.com/Factsheet/Factsheet_Nifty500LowVolatility50.pdf",
        "Nifty500 Momentum 50": "https://www.niftyindices.com/Factsheet/FactsheetNifty500Momentum50.pdf",
        "Nifty500 Quality 50": "https://niftyindices.com/Factsheet/Factsheet_Nifty500Quality50.pdf",
        "Nifty500 Multifactor MQVLv 50": "https://niftyindices.com/Factsheet/Factsheet_Nifty500MultifactorMQVLv50.pdf",
        "Nifty200 Quality 30": "https://www.niftyindices.com/Factsheet/Factsheet_NIFTY200_Quality30.pdf",
        "Nifty MidSmallcap400 Momentum Quality 100": "https://www.niftyindices.com/Factsheet/Factsheet_NiftyMidSmallcap400MomentumQuality100.pdf",
        "Nifty Smallcap250 Quality 50": "https://www.niftyindices.com/Factsheet/Factsheet_NiftySmallcap250Quality50.pdf",
        "Nifty Total Market Momentum Quality 50": "https://niftyindices.com/Factsheet/Factsheet_NiftyTotalMarketMomentumQuality50.pdf"
    }

```

#### `nselib/libutil.py`

```python
import os
from datetime import datetime, timedelta, date
import requests
import numpy as np
import pandas as pd
import logging
from nselib.constants import equity_periods, dd_mm_yyyy
import pandas_market_calendars as mcal
from nselib.errors import CalenderNotFound

logger = logging.getLogger(__name__)

default_header = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
}

header = {
    "referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Sec-Fetch-User": "?1",
    "Accept": "ext/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}

nse_calendar = mcal.get_calendar("NSE")


def validate_param_from_list(value: str, static_options_list: list):
    if value not in static_options_list:
        raise ValueError(
            f"'{value}' not a valid parameter :: select from {static_options_list}"
        )
    else:
        pass


def validate_date_param(from_date: str, to_date: str, period: str):
    if not period and (not from_date or not to_date):
        raise ValueError(" Please provide the valid parameters")
    elif period and period.upper() not in equity_periods:
        raise ValueError(f"period = {period} is not a valid value")

    try:
        if not period:
            from_date = datetime.strptime(from_date, dd_mm_yyyy)
            to_date = datetime.strptime(to_date, dd_mm_yyyy)
            time_delta = (to_date - from_date).days
            if time_delta < 1:
                raise ValueError("to_date should greater than from_date")
    except Exception as e:
        print(e)
        raise ValueError(
            f"either or both from_date = {from_date} || to_date = {to_date} are not valid value"
        )


def subtract_months(dt, months):
    # calculate target year and month
    year = dt.year
    month = dt.month - months

    # adjust year and month when month < 1
    while month <= 0:
        month += 12
        year -= 1

    # maximum days in each month
    month_days = [
        31,
        29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]

    # adjust the day if current date > max days in target month
    day = min(dt.day, month_days[month - 1])

    return date(year, month, day)


def derive_from_and_to_date(
    from_date: str = None, to_date: str = None, period: str = None
):
    """
    Derive the 'from' and 'to' dates based on the provided period.
    Adjusts dates to ensure they fall on a valid trading day.

    Args:
        from_date (str, optional): Start date in 'dd-mm-YYYY' format.
        to_date (str, optional): End date in 'dd-mm-YYYY' format.
        period (str, optional): Period for which to derive dates (e.g., '1D', '1W', '1M', '6M', '1Y').

    Returns:
        tuple: A tuple containing (from_date, to_date) in 'dd-mm-YYYY' format.

    Example:
            from nselib import libutil
            f_date, t_date = libutil.derive_from_and_to_date(period='1M')
    """
    if not period:
        logger.debug(
            f"No period provided, using from_date={from_date} and to_date={to_date}"
        )
        return from_date, to_date
    today = date.today()
    conditions = [
        period.upper() == "1D",
        period.upper() == "1W",
        period.upper() == "1M",
        period.upper() == "6M",
        period.upper() == "1Y",
    ]
    value = [
        today - timedelta(days=1),
        today - timedelta(weeks=1),
        subtract_months(today, 1),
        subtract_months(today, 6),
        subtract_months(today, 12),
    ]

    f_date = np.select(conditions, value, default=(today - timedelta(days=1)))
    f_date = pd.to_datetime(str(f_date))
    logger.debug(f"Derived initial f_date: {f_date} for period: {period}")
    while True:
        date_chk = nse_calendar.schedule(start_date=f_date, end_date=f_date)
        if not date_chk.empty:  # If market was open on this day
            break  # Stop the loop
        f_date -= timedelta(days=1)
    from_date = f_date.strftime(dd_mm_yyyy)
    today_str = today.strftime(dd_mm_yyyy)
    logger.debug(f"Final derived from_date: {from_date}, to_date: {today_str}")
    return from_date, today_str


def cleaning_column_name(col: list):
    unwanted_str_list = ["FH_", "EOD_", "HIT_"]
    new_col = col
    for unwanted in unwanted_str_list:
        new_col = [name.replace(f"{unwanted}", "") for name in new_col]
    return new_col


def cleaning_nse_symbol(symbol):
    symbol = symbol.replace("&", "%26")  # URL Parse for Stocks Like M&M Finance
    return symbol.upper()


def nse_urlfetch(url, origin_url="http://nseindia.com"):
    """
    Fetch data from an NSE URL using a session that mimics a real browser.

    Args:
        url (str): The target NSE API URL.
        origin_url (str, optional): The origin URL to fetch cookies from initially. Defaults to "http://nseindia.com".

    Returns:
        requests.Response: The HTTP response object.

    Example:
            from nselib import libutil
            response = libutil.nse_urlfetch('https://www.nseindia.com/api/holiday-master?type=trading')
    """
    logger.debug(f"Fetching cookies from origin_url: {origin_url}")
    r_session = requests.session()
    nse_live = r_session.get(origin_url, headers=default_header)
    cookies = nse_live.cookies
    logger.debug(f"Fetching data from url: {url}")
    return r_session.get(url, headers=header, cookies=cookies)


def get_nselib_path():
    """
    Extract isap file path
    """
    mydir = os.getcwd()
    return mydir.split(r"\nselib", 1)[0]


def get_month_from_date(trade_date):
    """
    get the month
    :param trade_date:
    :return: 'Dec'
    """
    return datetime.strptime(trade_date, "%Y-%m-%d").strftime("%b")


def trading_holiday_calendar():
    data_df = pd.DataFrame(
        columns=["Product", "tradingDate", "weekDay", "description", "Sr_no"]
    )
    url = "https://www.nseindia.com/api/holiday-master?type=trading"
    try:
        data_dict = nse_urlfetch(url).json()
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}", exc_info=e)
        raise CalenderNotFound("Calender data Not found try after some time")

    for prod in data_dict:
        h_df = pd.DataFrame(data_dict[prod])
        h_df["Product"] = prod
        data_df = pd.concat([data_df, h_df])
    condition = [
        data_df["Product"] == "CBM",
        data_df["Product"] == "CD",
        data_df["Product"] == "CM",
        data_df["Product"] == "CMOT",
        data_df["Product"] == "COM",
        data_df["Product"] == "FO",
        data_df["Product"] == "IRD",
        data_df["Product"] == "MF",
        data_df["Product"] == "NDM",
        data_df["Product"] == "NTRP",
        data_df["Product"] == "SLBS",
    ]
    value = [
        "Corporate Bonds",
        "Currency Derivatives",
        "Equities",
        "CMOT",
        "Commodity Derivatives",
        "Equity Derivatives",
        "Interest Rate Derivatives",
        "Mutual Funds",
        "New Debt Segment",
        "Negotiated Trade Reporting Platform",
        "Securities Lending & Borrowing Schemes",
    ]
    data_df["Product"] = np.select(condition, value, default="Unknown")
    return data_df


# if __name__ == "__main__":
#     data = derive_from_and_to_date('6M')
#     print(trading_holiday_calendar())

```

#### `nselib/logger.py`

```python
import logging
import sys


def enable_logging(level=logging.DEBUG):
    """
    Optional helper function for developers to quickly view nselib logs to the console.
    """
    logger = logging.getLogger("nselib")
    logger.setLevel(level)

    # Check if we already have a StreamHandler to avoid duplicate logs
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

```

#### `nselib/mutual_funds/__init__.py`

```python
from .mutual_fund_data import amfi_monthly_data, amfi_monthly_historical_data, amfi_monthly_report_links


```

#### `nselib/mutual_funds/mutual_fund_data.py`

```python
from __future__ import annotations

import io
import json
import re
from datetime import date
from urllib.parse import urljoin

import pandas as pd
import requests

from nselib.libutil import NSEdataNotFound, default_header

AMFI_MONTHLY_PAGE_URL = "https://www.amfiindia.com/research-information/amfi-monthly"
_AMFI_REPORT_LINK_PATTERN = re.compile(
    r"/spages/am([a-z]+)(\d{4})repo(?:revised)?\.(pdf|xls|xlsx|htm|html)$",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_DEFAULT_FILE_TYPE_PRIORITY = ("pdf", "xls", "xlsx", "html", "htm")
_OUTPUT_COLUMNS = [
    "REPORT_MONTH",
    "PERIOD_LABEL",
    "SOURCE_URL",
    "SOURCE_FILE_TYPE",
    "SECTION_NAME",
    "TABLE_INDEX",
    "ROW_INDEX",
    "ROW_JSON",
    "ROW_TEXT",
]


def _amfi_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(default_header)
    return session


def _normalize_report_month(value: str | date) -> date:
    parsed = pd.to_datetime(value, dayfirst=True, errors="raise").date()
    return parsed.replace(day=1)


def _empty_output_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_OUTPUT_COLUMNS)


def _extract_link_records(page_html: str) -> list[dict[str, object]]:
    hrefs = set(re.findall(r'href=["\']([^"\']+)["\']', page_html, flags=re.IGNORECASE))
    records: list[dict[str, object]] = []
    for href in hrefs:
        normalized_url = urljoin(AMFI_MONTHLY_PAGE_URL, href.strip())
        match = _AMFI_REPORT_LINK_PATTERN.search(normalized_url)
        if match is None:
            continue
        month_token = match.group(1).lower()
        month_value = _MONTH_MAP.get(month_token)
        if month_value is None:
            continue
        year_value = int(match.group(2))
        file_type = match.group(3).lower()
        report_month = date(year_value, month_value, 1)
        records.append(
            {
                "REPORT_MONTH": report_month,
                "PERIOD_LABEL": report_month.strftime("%b-%Y"),
                "REPORT_YEAR": year_value,
                "REPORT_MONTH_NUM": month_value,
                "FILE_TYPE": file_type,
                "IS_REVISED": "reporevised." in normalized_url.lower(),
                "SOURCE_URL": normalized_url,
            }
        )
    return records


def _priority_rank(file_type: object, file_type_priority: tuple[str, ...]) -> int:
    normalized = str(file_type).strip().lower()
    try:
        return file_type_priority.index(normalized)
    except ValueError:
        return len(file_type_priority)


def _preferred_links(links_frame: pd.DataFrame, file_type_priority: tuple[str, ...]) -> pd.DataFrame:
    if links_frame.empty:
        return links_frame
    ordered = links_frame.copy()
    ordered["__priority"] = ordered["FILE_TYPE"].apply(lambda value: _priority_rank(value, file_type_priority))
    ordered = ordered.sort_values(
        by=["REPORT_MONTH", "__priority", "IS_REVISED", "SOURCE_URL"],
        ascending=[True, True, False, True],
    )
    ordered = ordered.drop_duplicates(subset=["REPORT_MONTH"], keep="first").reset_index(drop=True)
    return ordered.drop(columns=["__priority"])


def _download_report_content(source_url: str) -> bytes:
    session = _amfi_session()
    response = session.get(source_url, timeout=90)
    if response.status_code != 200:
        raise NSEdataNotFound(f"AMFI report URL not reachable: {source_url} (status={response.status_code})")
    return response.content


def _coerce_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text_value = str(value).replace("\u00a0", " ").strip()
    if not text_value or text_value.lower() in {"nan", "none"}:
        return None
    return text_value


def _row_payload_from_values(values: list[object] | tuple[object, ...]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for index, value in enumerate(values, start=1):
        text_value = _coerce_text(value)
        if text_value is not None:
            payload[f"C{index}"] = text_value
    return payload


def _build_output_row(
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
    section_name: str,
    table_index: int,
    row_index: int,
    row_payload: dict[str, str],
) -> dict[str, object] | None:
    if not row_payload:
        return None
    row_text = " | ".join(row_payload.values())
    return {
        "REPORT_MONTH": report_month,
        "PERIOD_LABEL": period_label,
        "SOURCE_URL": source_url,
        "SOURCE_FILE_TYPE": source_file_type,
        "SECTION_NAME": section_name,
        "TABLE_INDEX": int(table_index),
        "ROW_INDEX": int(row_index),
        "ROW_JSON": json.dumps(row_payload, ensure_ascii=False),
        "ROW_TEXT": row_text,
    }


def _parse_excel_report(
    report_content: bytes,
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
) -> pd.DataFrame:
    engine = "xlrd" if source_file_type == "xls" else None
    sheets = pd.read_excel(
        io.BytesIO(report_content),
        sheet_name=None,
        header=None,
        dtype=object,
        engine=engine,
    )
    rows: list[dict[str, object]] = []
    for sheet_name, sheet_frame in sheets.items():
        if sheet_frame.empty:
            continue
        for row_index, row_values in enumerate(sheet_frame.itertuples(index=False, name=None), start=1):
            payload = _row_payload_from_values(row_values)
            output_row = _build_output_row(
                report_month=report_month,
                period_label=period_label,
                source_url=source_url,
                source_file_type=source_file_type,
                section_name=f"Sheet:{sheet_name}",
                table_index=1,
                row_index=row_index,
                row_payload=payload,
            )
            if output_row is not None:
                rows.append(output_row)
    if not rows:
        return _empty_output_frame()
    return pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)


def _parse_html_report(
    report_content: bytes,
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
) -> pd.DataFrame:
    html_text = report_content.decode("utf-8", errors="ignore")
    tables = pd.read_html(io.StringIO(html_text), header=None)
    rows: list[dict[str, object]] = []
    for table_index, table in enumerate(tables, start=1):
        if table.empty:
            continue
        for row_index, row_values in enumerate(table.itertuples(index=False, name=None), start=1):
            payload = _row_payload_from_values(row_values)
            output_row = _build_output_row(
                report_month=report_month,
                period_label=period_label,
                source_url=source_url,
                source_file_type=source_file_type,
                section_name=f"Table:{table_index}",
                table_index=table_index,
                row_index=row_index,
                row_payload=payload,
            )
            if output_row is not None:
                rows.append(output_row)
    if not rows:
        return _empty_output_frame()
    return pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)


def _parse_pdf_report_with_pdfplumber(
    report_content: bytes,
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
) -> pd.DataFrame:
    import pdfplumber

    rows: list[dict[str, object]] = []
    with pdfplumber.open(io.BytesIO(report_content)) as pdf_file:
        for page_index, page in enumerate(pdf_file.pages, start=1):
            extracted_tables = page.extract_tables() or []
            if extracted_tables:
                for table_index, table in enumerate(extracted_tables, start=1):
                    global_table_index = page_index * 1000 + table_index
                    for row_index, row_values in enumerate(table or [], start=1):
                        payload = _row_payload_from_values(row_values or [])
                        output_row = _build_output_row(
                            report_month=report_month,
                            period_label=period_label,
                            source_url=source_url,
                            source_file_type=source_file_type,
                            section_name=f"Page:{page_index}",
                            table_index=global_table_index,
                            row_index=row_index,
                            row_payload=payload,
                        )
                        if output_row is not None:
                            rows.append(output_row)
                continue

            page_text = page.extract_text() or ""
            for row_index, line in enumerate(page_text.splitlines(), start=1):
                payload = _row_payload_from_values([line])
                output_row = _build_output_row(
                    report_month=report_month,
                    period_label=period_label,
                    source_url=source_url,
                    source_file_type=source_file_type,
                    section_name=f"Page:{page_index}",
                    table_index=page_index * 1000,
                    row_index=row_index,
                    row_payload=payload,
                )
                if output_row is not None:
                    rows.append(output_row)

    if not rows:
        return _empty_output_frame()
    return pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)


def _parse_pdf_report_with_pypdf(
    report_content: bytes,
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
) -> pd.DataFrame:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(report_content))
    rows: list[dict[str, object]] = []
    for page_index, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        for row_index, line in enumerate(page_text.splitlines(), start=1):
            payload = _row_payload_from_values([line])
            output_row = _build_output_row(
                report_month=report_month,
                period_label=period_label,
                source_url=source_url,
                source_file_type=source_file_type,
                section_name=f"Page:{page_index}",
                table_index=page_index * 1000,
                row_index=row_index,
                row_payload=payload,
            )
            if output_row is not None:
                rows.append(output_row)

    if not rows:
        return _empty_output_frame()
    return pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)


def _parse_pdf_report(
    report_content: bytes,
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
) -> pd.DataFrame:
    try:
        return _parse_pdf_report_with_pdfplumber(
            report_content=report_content,
            report_month=report_month,
            period_label=period_label,
            source_url=source_url,
            source_file_type=source_file_type,
        )
    except Exception:  # noqa: BLE001
        try:
            return _parse_pdf_report_with_pypdf(
                report_content=report_content,
                report_month=report_month,
                period_label=period_label,
                source_url=source_url,
                source_file_type=source_file_type,
            )
        except Exception as exc:  # noqa: BLE001
            raise NSEdataNotFound(f"Unable to parse AMFI PDF report: {source_url} :: {exc}")


def _parse_report_content(
    report_content: bytes,
    report_month: date,
    period_label: str,
    source_url: str,
    source_file_type: str,
) -> pd.DataFrame:
    if source_file_type in {"xls", "xlsx"}:
        return _parse_excel_report(
            report_content=report_content,
            report_month=report_month,
            period_label=period_label,
            source_url=source_url,
            source_file_type=source_file_type,
        )
    if source_file_type in {"html", "htm"}:
        return _parse_html_report(
            report_content=report_content,
            report_month=report_month,
            period_label=period_label,
            source_url=source_url,
            source_file_type=source_file_type,
        )
    if source_file_type == "pdf":
        return _parse_pdf_report(
            report_content=report_content,
            report_month=report_month,
            period_label=period_label,
            source_url=source_url,
            source_file_type=source_file_type,
        )
    raise NSEdataNotFound(f"Unsupported AMFI monthly report type: {source_file_type}")


def amfi_monthly_report_links() -> pd.DataFrame:
    """
    List all report links exposed on the AMFI monthly archive page.
    """
    response = _amfi_session().get(AMFI_MONTHLY_PAGE_URL, timeout=60)
    if response.status_code != 200:
        raise NSEdataNotFound(f"Unable to access AMFI monthly archive page (status={response.status_code})")
    records = _extract_link_records(response.text)
    if not records:
        return pd.DataFrame(
            columns=[
                "REPORT_MONTH",
                "PERIOD_LABEL",
                "REPORT_YEAR",
                "REPORT_MONTH_NUM",
                "FILE_TYPE",
                "IS_REVISED",
                "SOURCE_URL",
            ]
        )
    frame = pd.DataFrame(records).drop_duplicates(
        subset=["REPORT_MONTH", "FILE_TYPE", "SOURCE_URL"],
        keep="first",
    )
    frame = frame.sort_values(by=["REPORT_MONTH", "FILE_TYPE", "SOURCE_URL"]).reset_index(drop=True)
    return frame[
        [
            "REPORT_MONTH",
            "PERIOD_LABEL",
            "REPORT_YEAR",
            "REPORT_MONTH_NUM",
            "FILE_TYPE",
            "IS_REVISED",
            "SOURCE_URL",
        ]
    ]


def amfi_monthly_data(
    report_month: str | date,
    file_type_priority: tuple[str, ...] | list[str] | None = None,
) -> pd.DataFrame:
    """
    Fetch a single month AMFI report and return normalized row-wise records.
    """
    normalized_month = _normalize_report_month(report_month)
    priority = tuple(str(value).strip().lower() for value in (file_type_priority or _DEFAULT_FILE_TYPE_PRIORITY))
    links_frame = amfi_monthly_report_links()
    month_links = links_frame[links_frame["REPORT_MONTH"] == normalized_month].copy()
    if month_links.empty:
        return _empty_output_frame()
    selected_link = _preferred_links(month_links, priority).iloc[0]
    source_url = str(selected_link["SOURCE_URL"]).strip()
    source_file_type = str(selected_link["FILE_TYPE"]).strip().lower()
    period_label = str(selected_link["PERIOD_LABEL"]).strip()
    content = _download_report_content(source_url)
    parsed = _parse_report_content(
        report_content=content,
        report_month=normalized_month,
        period_label=period_label,
        source_url=source_url,
        source_file_type=source_file_type,
    )
    return parsed[_OUTPUT_COLUMNS] if not parsed.empty else _empty_output_frame()


def amfi_monthly_historical_data(
    from_month: str | date | None = None,
    to_month: str | date | None = None,
    file_type_priority: tuple[str, ...] | list[str] | None = None,
    include_all_file_variants: bool = False,
    strict: bool = False,
) -> pd.DataFrame:
    """
    Fetch AMFI monthly reports for a historical month range.
    """
    priority = tuple(str(value).strip().lower() for value in (file_type_priority or _DEFAULT_FILE_TYPE_PRIORITY))
    links_frame = amfi_monthly_report_links()
    if links_frame.empty:
        return _empty_output_frame()

    if from_month is not None:
        from_anchor = _normalize_report_month(from_month)
        links_frame = links_frame[links_frame["REPORT_MONTH"] >= from_anchor].copy()
    if to_month is not None:
        to_anchor = _normalize_report_month(to_month)
        links_frame = links_frame[links_frame["REPORT_MONTH"] <= to_anchor].copy()
    if links_frame.empty:
        return _empty_output_frame()

    if include_all_file_variants:
        selected_links = links_frame.sort_values(by=["REPORT_MONTH", "FILE_TYPE", "SOURCE_URL"]).reset_index(drop=True)
    else:
        selected_links = _preferred_links(links_frame, priority)

    output_frames: list[pd.DataFrame] = []
    for report_month in selected_links["REPORT_MONTH"].drop_duplicates().tolist():
        try:
            month_frame = amfi_monthly_data(report_month=report_month, file_type_priority=priority)
        except Exception:
            if strict:
                raise
            continue
        if month_frame.empty:
            continue
        output_frames.append(month_frame)

    if not output_frames:
        return _empty_output_frame()
    return pd.concat(output_frames, ignore_index=True)[_OUTPUT_COLUMNS]


```

#### `nselib/nsdl_fpi.py`

```python
from __future__ import annotations

import atexit
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
import os
import re

import pandas as pd
import requests

from nselib.libutil import default_header

REPORT_DATE_FORMAT = "%d-%b-%Y"
PRODUCTION_BASE_URL = "https://www.fpi.nsdl.co.in/web/Reports"
PILOT_BASE_URL = "https://pilot.fpi.nsdl.co.in/Reports"
REQUEST_BASE_URLS = [PILOT_BASE_URL, PRODUCTION_BASE_URL]
LATEST_PAGE = "Latest.aspx"
ARCHIVE_PAGE = "Archive.aspx"
ARCHIVE_HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
BROWSER_EXECUTABLE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
BROWSER_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
]
BROWSER_NAVIGATION_OPTIONS = {"waitUntil": "networkidle2", "timeout": 60000}
BROWSER_USER_AGENT = default_header.get(
    "User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
)

INVESTMENT_COLUMNS = [
    "REPORT_DATE",
    "ASSET_CLASS",
    "INVESTMENT_ROUTE",
    "GROSS_PURCHASES_RS_CR",
    "GROSS_SALES_RS_CR",
    "NET_INVESTMENT_RS_CR",
    "NET_INVESTMENT_USD_MN",
    "USD_INR_CONVERSION",
]

DERIVATIVE_COLUMNS = [
    "REPORT_DATE",
    "DERIVATIVE_PRODUCT",
    "BUY_CONTRACTS",
    "BUY_AMOUNT_CR",
    "SELL_CONTRACTS",
    "SELL_AMOUNT_CR",
    "OPEN_INTEREST_CONTRACTS",
    "OPEN_INTEREST_AMOUNT_CR",
]

DAILY_DERIVATIVE_PRODUCTS = {
    "Index Futures",
    "Index Options",
    "Stock Futures",
    "Stock Options",
    "Interest Rate Futures",
    "Currency Futures",
    "Currency Options",
    "Commodity Futures",
    "Commodity Options",
}


@dataclass
class NSDLFPIReportBundle:
    investment: pd.DataFrame
    derivative: pd.DataFrame
    source_page: str
    as_of_date: date | None = None


def _empty_investment_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=INVESTMENT_COLUMNS)


def _empty_derivative_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=DERIVATIVE_COLUMNS)


def _coerce_trade_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.to_datetime(value, dayfirst=True, errors="raise").date()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid NSDL trade date: {value}") from exc


def _flatten_column_name(column: object) -> str:
    if isinstance(column, tuple):
        parts = [
            str(part).strip()
            for part in column
            if part is not None
            and str(part).strip()
            and not str(part).strip().lower().startswith("unnamed:")
        ]
        return " | ".join(parts)
    return str(column).strip()


def _normalize_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _parse_numeric(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    text = text.replace("Rs.", "").replace("Rs", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _find_browser_executable() -> str | None:
    for candidate in BROWSER_EXECUTABLE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _extract_hidden_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for field_name in ARCHIVE_HIDDEN_FIELDS:
        match = re.search(
            rf'name="{field_name}"\s+id="{field_name}"\s+value="([^"]*)"',
            html,
        )
        fields[field_name] = match.group(1) if match else ""
    return fields


def _read_tables(html: str) -> list[pd.DataFrame]:
    try:
        return pd.read_html(StringIO(html))
    except ValueError:
        return []


def _classify_table(table: pd.DataFrame) -> str | None:
    column_names = [_flatten_column_name(column).lower() for column in table.columns]
    if any("investment route" in column_name for column_name in column_names):
        return "investment"
    if any("derivative products" in column_name for column_name in column_names):
        return "derivative"
    return None


def _parse_investment_table(table: pd.DataFrame) -> pd.DataFrame:
    frame = table.copy()
    frame.columns = [_flatten_column_name(column) for column in frame.columns]
    frame = frame.dropna(how="all").reset_index(drop=True)
    rename_map: dict[str, str] = {}
    for column_name in frame.columns:
        normalized = column_name.lower()
        if "reporting date" in normalized:
            rename_map[column_name] = "REPORT_DATE"
        elif "investment route" in normalized:
            rename_map[column_name] = "INVESTMENT_ROUTE"
        elif "gross purchases" in normalized:
            rename_map[column_name] = "GROSS_PURCHASES_RS_CR"
        elif "gross sales" in normalized:
            rename_map[column_name] = "GROSS_SALES_RS_CR"
        elif "net investment" in normalized and "us($" not in normalized:
            rename_map[column_name] = "NET_INVESTMENT_RS_CR"
        elif "net investment us($" in normalized:
            rename_map[column_name] = "NET_INVESTMENT_USD_MN"
        elif "conversion" in normalized:
            rename_map[column_name] = "USD_INR_CONVERSION"
        elif "debt" in normalized and "equity" in normalized:
            rename_map[column_name] = "ASSET_CLASS"
    frame = frame.rename(columns=rename_map)
    available_columns = [column_name for column_name in INVESTMENT_COLUMNS if column_name in frame.columns]
    if not available_columns:
        return _empty_investment_frame()
    frame = frame[available_columns].copy()
    for column_name in INVESTMENT_COLUMNS:
        if column_name not in frame.columns:
            frame[column_name] = None
    parsed_dates = pd.to_datetime(frame["REPORT_DATE"], format=REPORT_DATE_FORMAT, errors="coerce").ffill()
    frame["REPORT_DATE"] = parsed_dates.dt.date
    frame["ASSET_CLASS"] = frame["ASSET_CLASS"].apply(_normalize_text)
    frame["INVESTMENT_ROUTE"] = frame["INVESTMENT_ROUTE"].apply(_normalize_text)
    for column_name in [
        "GROSS_PURCHASES_RS_CR",
        "GROSS_SALES_RS_CR",
        "NET_INVESTMENT_RS_CR",
        "NET_INVESTMENT_USD_MN",
        "USD_INR_CONVERSION",
    ]:
        frame[column_name] = frame[column_name].apply(_parse_numeric)
    frame = frame.dropna(subset=["REPORT_DATE", "ASSET_CLASS"]).copy()
    frame = frame[frame["ASSET_CLASS"].str.lower() != "note"].copy()
    frame = frame[frame["USD_INR_CONVERSION"].notna()].copy()
    frame = frame[INVESTMENT_COLUMNS].drop_duplicates(
        subset=["REPORT_DATE", "ASSET_CLASS", "INVESTMENT_ROUTE"],
        keep="first",
    ).reset_index(drop=True)
    return frame


def _parse_derivative_table(table: pd.DataFrame) -> pd.DataFrame:
    frame = table.copy()
    frame.columns = [_flatten_column_name(column) for column in frame.columns]
    frame = frame.dropna(how="all").reset_index(drop=True)
    rename_map: dict[str, str] = {}
    for column_name in frame.columns:
        normalized = column_name.lower()
        if "reporting date" in normalized:
            rename_map[column_name] = "REPORT_DATE"
        elif "derivative products" in normalized:
            rename_map[column_name] = "DERIVATIVE_PRODUCT"
        elif "buy" in normalized and "no. of contracts" in normalized:
            rename_map[column_name] = "BUY_CONTRACTS"
        elif "buy" in normalized and "amount in crore" in normalized:
            rename_map[column_name] = "BUY_AMOUNT_CR"
        elif "sell" in normalized and "no. of contracts" in normalized:
            rename_map[column_name] = "SELL_CONTRACTS"
        elif "sell" in normalized and "amount in crore" in normalized:
            rename_map[column_name] = "SELL_AMOUNT_CR"
        elif "open interest" in normalized and "no. of contracts" in normalized:
            rename_map[column_name] = "OPEN_INTEREST_CONTRACTS"
        elif "open interest" in normalized and "amount in crore" in normalized:
            rename_map[column_name] = "OPEN_INTEREST_AMOUNT_CR"
    frame = frame.rename(columns=rename_map)
    available_columns = [column_name for column_name in DERIVATIVE_COLUMNS if column_name in frame.columns]
    if not available_columns:
        return _empty_derivative_frame()
    frame = frame[available_columns].copy()
    for column_name in DERIVATIVE_COLUMNS:
        if column_name not in frame.columns:
            frame[column_name] = None
    parsed_dates = pd.to_datetime(frame["REPORT_DATE"], format=REPORT_DATE_FORMAT, errors="coerce").ffill()
    frame["REPORT_DATE"] = parsed_dates.dt.date
    frame["DERIVATIVE_PRODUCT"] = frame["DERIVATIVE_PRODUCT"].apply(_normalize_text)
    frame["DERIVATIVE_PRODUCT"] = (
        frame["DERIVATIVE_PRODUCT"]
        .fillna("")
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
        .str.title()
    )
    for column_name in [
        "BUY_CONTRACTS",
        "BUY_AMOUNT_CR",
        "SELL_CONTRACTS",
        "SELL_AMOUNT_CR",
        "OPEN_INTEREST_CONTRACTS",
        "OPEN_INTEREST_AMOUNT_CR",
    ]:
        frame[column_name] = frame[column_name].apply(_parse_numeric)
    frame = frame.dropna(subset=["REPORT_DATE", "DERIVATIVE_PRODUCT"]).copy()
    frame = frame[frame["DERIVATIVE_PRODUCT"].isin(DAILY_DERIVATIVE_PRODUCTS)].copy()
    frame = frame[DERIVATIVE_COLUMNS].drop_duplicates(
        subset=["REPORT_DATE", "DERIVATIVE_PRODUCT"],
        keep="first",
    ).reset_index(drop=True)
    return frame


def _parse_report_bundle(html: str, source_page: str, as_of_date: date | None) -> NSDLFPIReportBundle:
    investment = _empty_investment_frame()
    derivative = _empty_derivative_frame()
    for table in _read_tables(html):
        table_type = _classify_table(table)
        if table_type == "investment":
            investment = _parse_investment_table(table)
        elif table_type == "derivative":
            derivative = _parse_derivative_table(table)
    return NSDLFPIReportBundle(
        investment=investment,
        derivative=derivative,
        source_page=source_page,
        as_of_date=as_of_date,
    )


def _max_bundle_report_date(bundle: NSDLFPIReportBundle) -> date | None:
    dates: list[date] = []
    for frame in (bundle.investment, bundle.derivative):
        if frame.empty or "REPORT_DATE" not in frame.columns:
            continue
        valid = [value for value in frame["REPORT_DATE"].tolist() if isinstance(value, date)]
        if valid:
            dates.append(max(valid))
    if not dates:
        return None
    return max(dates)


class NSDLProductionBrowser:
    def __init__(self) -> None:
        self.executable_path = _find_browser_executable()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._browser = None
        self._atexit_registered = False

    @property
    def available(self) -> bool:
        return bool(self.executable_path)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coroutine):
        loop = self._ensure_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coroutine)

    async def _ensure_browser(self):
        if not self.available or not self.executable_path:
            raise FileNotFoundError("No supported Edge/Chrome executable was found for NSDL production scraping")
        if self._browser is not None:
            process = getattr(self._browser, "process", None)
            if process is None or process.poll() is None:
                return self._browser
        from pyppeteer import launch

        self._browser = await launch(
            headless=True,
            executablePath=self.executable_path,
            args=BROWSER_LAUNCH_ARGS,
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
        )
        if not self._atexit_registered:
            atexit.register(self.close)
            self._atexit_registered = True
        return self._browser

    async def _new_page(self, url: str):
        browser = await self._ensure_browser()
        page = await browser.newPage()
        await page.setUserAgent(BROWSER_USER_AGENT)
        await page.goto(url, BROWSER_NAVIGATION_OPTIONS)
        return page

    async def _latest_html(self) -> str:
        page = await self._new_page(f"{PRODUCTION_BASE_URL}/{LATEST_PAGE}")
        try:
            return await page.content()
        finally:
            await page.close()

    async def _archive_html(self, trade_date: date) -> str:
        page = await self._new_page(f"{PRODUCTION_BASE_URL}/{ARCHIVE_PAGE}")
        try:
            trade_date_text = trade_date.strftime(REPORT_DATE_FORMAT)
            await page.evaluate(
                """(dateValue) => {
                    const txtDate = document.querySelector('#txtDate');
                    const hiddenDate = document.querySelector('#hdnDate');
                    if (!txtDate || !hiddenDate) {
                        throw new Error('NSDL archive date controls were not found');
                    }
                    txtDate.disabled = false;
                    txtDate.value = dateValue;
                    hiddenDate.value = dateValue;
                }""",
                trade_date_text,
            )
            await asyncio.gather(
                page.waitForNavigation(BROWSER_NAVIGATION_OPTIONS),
                page.click("#btnSubmit1"),
            )
            return await page.content()
        finally:
            await page.close()

    def latest_html(self) -> str:
        return self._run(self._latest_html())

    def archive_html(self, trade_date: date) -> str:
        return self._run(self._archive_html(trade_date))

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._run(self._browser.close())
        except Exception:
            pass
        finally:
            self._browser = None
        try:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
        except Exception:
            pass
        finally:
            self._loop = None


class NSDLFPIClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(default_header)
        self.browser = NSDLProductionBrowser()
        self._request_base_url: str | None = None
        self._archive_payload_fields: dict[str, str] | None = None

    def _resolve_request_base_url(self, page_name: str = LATEST_PAGE) -> str:
        if self._request_base_url is not None:
            return self._request_base_url
        last_error: Exception | None = None
        for base_url in REQUEST_BASE_URLS:
            try:
                response = self.session.get(f"{base_url}/{page_name}", timeout=60)
                response.raise_for_status()
                if "NSDL" not in response.text or "FPI" not in response.text:
                    continue
                self._request_base_url = base_url
                if page_name == ARCHIVE_PAGE:
                    self._archive_payload_fields = _extract_hidden_fields(response.text)
                return self._request_base_url
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise FileNotFoundError(f"Unable to reach NSDL FPI reports page: {last_error}")

    def _get_request_page(self, page_name: str) -> str:
        base_url = self._resolve_request_base_url(page_name)
        response = self.session.get(f"{base_url}/{page_name}", timeout=60)
        response.raise_for_status()
        return response.text

    def _archive_request_fields(self, refresh: bool = False) -> dict[str, str]:
        if self._archive_payload_fields is not None and not refresh:
            return self._archive_payload_fields
        html = self._get_request_page(ARCHIVE_PAGE)
        fields = _extract_hidden_fields(html)
        if not all(fields.values()):
            raise FileNotFoundError("Unable to prepare NSDL archive request")
        self._archive_payload_fields = fields
        return fields

    def _get_latest_html(self) -> tuple[str, str]:
        errors: list[str] = []
        if self.browser.available:
            try:
                return self.browser.latest_html(), PRODUCTION_BASE_URL
            except Exception as exc:  # noqa: BLE001
                errors.append(f"production_browser: {exc}")
        try:
            return self._get_request_page(LATEST_PAGE), self._resolve_request_base_url(LATEST_PAGE)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"request_fallback: {exc}")
        raise FileNotFoundError("Unable to fetch NSDL latest page :: " + " | ".join(errors))

    def _get_archive_html(self, trade_date: date) -> tuple[str, str]:
        errors: list[str] = []
        if self.browser.available:
            try:
                return self.browser.archive_html(trade_date), PRODUCTION_BASE_URL
            except Exception as exc:  # noqa: BLE001
                errors.append(f"production_browser: {exc}")

        try:
            fields = self._archive_request_fields()
            payload = {
                "__EVENTTARGET": "btnSubmit1",
                "__EVENTARGUMENT": "",
                "__VIEWSTATE": fields["__VIEWSTATE"],
                "__VIEWSTATEGENERATOR": fields["__VIEWSTATEGENERATOR"],
                "__EVENTVALIDATION": fields["__EVENTVALIDATION"],
                "txtDate": trade_date.strftime(REPORT_DATE_FORMAT),
                "hdnDate": trade_date.strftime(REPORT_DATE_FORMAT),
                "HdnValexceldata": "",
                "hdnFlag": "",
            }
            base_url = self._resolve_request_base_url(ARCHIVE_PAGE)
            response = self.session.post(
                f"{base_url}/{ARCHIVE_PAGE}",
                data=payload,
                timeout=60,
            )
            response.raise_for_status()
            return response.text, base_url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"request_fallback: {exc}")
            try:
                self._archive_request_fields(refresh=True)
            except Exception:
                pass
        raise FileNotFoundError("Unable to fetch NSDL archive page :: " + " | ".join(errors))

    def latest_bundle(self) -> NSDLFPIReportBundle:
        html, source_base = self._get_latest_html()
        source_page = "latest_production_browser" if source_base == PRODUCTION_BASE_URL else "latest_request_fallback"
        bundle = _parse_report_bundle(html, source_page=source_page, as_of_date=None)
        bundle.as_of_date = _max_bundle_report_date(bundle)
        if bundle.investment.empty and bundle.derivative.empty:
            raise FileNotFoundError("NSDL latest page did not contain investment or derivative data")
        return bundle

    def archive_month_bundle(self, trade_date: date | datetime | str, max_lookback_days: int = 10) -> NSDLFPIReportBundle:
        requested_date = _coerce_trade_date(trade_date)
        current_date = requested_date
        month_key = (requested_date.year, requested_date.month)
        last_error: Exception | None = None
        for _ in range(max_lookback_days + 1):
            if (current_date.year, current_date.month) != month_key:
                break
            try:
                html, source_base = self._get_archive_html(current_date)
                if "No Data To Display" not in html:
                    source_page = (
                        "archive_production_browser" if source_base == PRODUCTION_BASE_URL else "archive_request_fallback"
                    )
                    bundle = _parse_report_bundle(html, source_page=source_page, as_of_date=current_date)
                    if not bundle.investment.empty or not bundle.derivative.empty:
                        return bundle
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                try:
                    self._archive_request_fields(refresh=True)
                except Exception:  # noqa: BLE001
                    pass
            current_date -= timedelta(days=1)
        if last_error is not None:
            raise FileNotFoundError(
                f"NSDL archive data not available for month ending {requested_date.strftime(REPORT_DATE_FORMAT)}: {last_error}"
            )
        raise FileNotFoundError(
            f"NSDL archive data not available for month ending {requested_date.strftime(REPORT_DATE_FORMAT)}"
        )


def fetch_nsdl_fpi_latest_bundle() -> NSDLFPIReportBundle:
    return NSDLFPIClient().latest_bundle()


def fetch_nsdl_fpi_month_bundle(trade_date: date | datetime | str) -> NSDLFPIReportBundle:
    return NSDLFPIClient().archive_month_bundle(trade_date)


def fetch_nsdl_fpi_latest_investment_activity() -> pd.DataFrame:
    return fetch_nsdl_fpi_latest_bundle().investment.copy()


def fetch_nsdl_fpi_latest_derivative_activity() -> pd.DataFrame:
    return fetch_nsdl_fpi_latest_bundle().derivative.copy()


def fetch_nsdl_fpi_investment_activity(trade_date: date | datetime | str) -> pd.DataFrame:
    requested_date = _coerce_trade_date(trade_date)
    frame = fetch_nsdl_fpi_month_bundle(requested_date).investment
    frame = frame[frame["REPORT_DATE"] == requested_date].copy().reset_index(drop=True)
    if frame.empty:
        raise FileNotFoundError(f"NSDL investment activity not found for {requested_date.strftime(REPORT_DATE_FORMAT)}")
    return frame


def fetch_nsdl_fpi_derivative_activity(trade_date: date | datetime | str) -> pd.DataFrame:
    requested_date = _coerce_trade_date(trade_date)
    frame = fetch_nsdl_fpi_month_bundle(requested_date).derivative
    frame = frame[frame["REPORT_DATE"] == requested_date].copy().reset_index(drop=True)
    if frame.empty:
        raise FileNotFoundError(f"NSDL derivative activity not found for {requested_date.strftime(REPORT_DATE_FORMAT)}")
    return frame

```

