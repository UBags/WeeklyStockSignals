# pip install nselib
from nselib import capital_market

# Fetch comprehensive corporate actions for all NSE stocks
actions_df  = capital_market.corporate_actions_for_equity(from_date='01-01-2006', to_date='15-07-2026')
actions_df.to_csv('nse_corporate_actions.csv', index=False)

# import pandas as pd
# from nselib import capital_market
# from datetime import datetime, timedelta
# import time
#
# try:
#     eq_list = capital_market.equity_list()
#     print(eq_list)
# except Exception as e:
#     print(f"Could not fetch equity list due to {e}")
#
# # Define your full date range
# start_date_str = '01-01-2006'
# end_date_str = '15-07-2026'
#
# # Convert to datetime objects
# start_date = datetime.strptime(start_date_str, "%d-%m-%Y")
# end_date = datetime.strptime(end_date_str, "%d-%m-%Y")
#
# all_data = []
# current_date = start_date
#
# print(f"Fetching data from {start_date_str} to {end_date_str} in 1-year chunks...")
#
# while current_date < end_date:
#     # Define a 1-year chunk (or up to the end_date)
#     next_year = current_date + timedelta(days=365)
#     chunk_end = min(next_year, end_date)
#
#     # Format dates as strings for the API
#     f_start = current_date.strftime("%d-%m-%Y")
#     f_end = chunk_end.strftime("%d-%m-%Y")
#
#     try:
#         print(f"Fetching: {f_start} -> {f_end}")
#         df = capital_market.corporate_actions_for_equity(from_date=f_start, to_date=f_end)
#
#         # Check if data was actually returned before adding
#         if not df.empty:
#             all_data.append(df)
#
#     except Exception as e:
#         print(f"Skipping {f_start} to {f_end}: {e}")
#
#     # Move to the next day after the current chunk
#     current_date = chunk_end + timedelta(days=1)
#
#     # IMPORTANT: Pause briefly to avoid getting IP blocked by NSE
#     time.sleep(1)
#
# # Combine all chunks
# if all_data:
#     final_df = pd.concat(all_data, ignore_index=True)
#
#     # Clean up: Remove duplicates and sort
#     final_df.drop_duplicates(inplace=True)
#     # Convert 'ex_date' to datetime for sorting (handle mixed formats if necessary)
#     final_df['ex_date'] = pd.to_datetime(final_df['ex_date'], dayfirst=True, errors='coerce')
#     final_df = final_df.sort_values(by='ex_date', ascending=False)
#
#     print(f"Success! Total records fetched: {len(final_df)}")
#     print(final_df.head())
# else:
#     print("No data fetched. Check your internet connection or library version.")
