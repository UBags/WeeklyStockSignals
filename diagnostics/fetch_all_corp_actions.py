# pip install nselib
from nselib import capital_market
import pandas as pd
from pandas.core.interchange.dataframe_protocol import DataFrame

# Fetch comprehensive corporate actions for all NSE stocks
actions_df  = capital_market.corporate_actions_for_equity(from_date='01-01-2006', to_date='15-07-2026')
actions_df.to_csv('nse_corporate_actions.csv', index=False)
