import pandas as pd
import dataframe_image as dfi
from matplotlib.colors import LinearSegmentedColormap
from source_engine.opus_source import OpusSource

mandate = {
    'D&R Aktien': '17154631',
    'D&R Aktien Nachhaltigkeit': '79939969'
}

query = """
    SELECT
        accountsegments.name,
        accountsegments.account_id,
        accountsegments.nav,
        reportings.report_date, 
        positions.name as position_name,
        positions.asset_class,
        positions.value_base_ccy as value,
        positions.bloomberg_query,
        positions.underlying_name,
        positions.total_exposure,
        positions.last_xrate_quantity,
        positions.country_of_domicile as country,
        positions.gics_industry_sector as Sector
    FROM
        reportings
            JOIN
        accountsegments ON (accountsegments.reporting_uuid = reportings.uuid)
            JOIN
        positions ON (reportings.uuid = positions.reporting_uuid)
    WHERE
            positions.account_segment_id = accountsegments.accountsegment_id
            AND reportings.newest = 1
            AND reportings.report = 'positions'
            AND positions.asset_class in ('STOCK', 'FUTURE')
            AND positions.dr_class_level_1 = 'EQUITY'
            AND accountsegments.accountsegment_id in ('{mandate}')
            AND reportings.report_date = (SELECT
                                            MAX(report_date)
                                          FROM
                                            reportings)
    """

opus = OpusSource()


def get_benchmark_positions() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sxxp = pd.read_excel('data.xlsx', sheet_name="SXXP Index", header=0, index_col=0)
    spx = pd.read_excel('data.xlsx', sheet_name="SPX Index", header=0, index_col=0)

    sxxp = calculate_initials_weights(sxxp)
    spx = calculate_initials_weights(spx)

    sxxp = calculate_current_weights(sxxp)
    spx = calculate_current_weights(spx)

    benchmark = combine_benchmarks(sxxp, spx, sxxp_weight=0.6, spx_weight=0.4)

    return sxxp, spx, benchmark


def combine_benchmarks(sxxp: pd.DataFrame, spx: pd.DataFrame, sxxp_weight: float = 0.6,
                       spx_weight: float = 0.4) -> pd.DataFrame:
    sxxp = sxxp.copy()
    spx = spx.copy()
    sxxp['initial_weight'] = sxxp['initial_weight'] * sxxp_weight
    spx['initial_weight'] = spx['initial_weight'] * spx_weight

    sxxp['current_weight'] = sxxp['current_weight'] * sxxp_weight
    spx['current_weight'] = spx['current_weight'] * spx_weight

    benchmark = pd.concat([sxxp, spx])

    return benchmark


def group(df: pd.DataFrame, group_by_col: str, sum_col: str) -> pd.DataFrame:
    grouped = df.groupby(group_by_col).agg({
        sum_col: 'sum'
    })

    grouped = grouped.reset_index()
    grouped.set_index(group_by_col, inplace=True)

    return grouped


def calculate_initials_weights(df: pd.DataFrame) -> pd.DataFrame:
    num_stocks = len(df)
    equal_weight = 1 / num_stocks

    cap_factor = 100_000_000_000.0

    amount = cap_factor * equal_weight

    df['num_stocks'] = (amount / df['#px_close']).astype(int)
    df['initial_value'] = df['num_stocks'] * df['#px_close']

    total_initial_value = df['initial_value'].sum()
    df['initial_weight'] = df['initial_value'] / total_initial_value * 100

    return df


def calculate_current_weights(df: pd.DataFrame) -> pd.DataFrame:
    df['current_value'] = df['num_stocks'] * df['#px_last']

    total_current_value = df['current_value'].sum()
    df['current_weight'] = df['current_value'] / total_current_value * 100

    return df


def get_account_positions(id: str) -> pd.DataFrame:
    df = opus.read_sql(query=query.format(mandate=id))
    df.set_index('position_name', inplace=True)
    df['Exposure'] = (df['total_exposure'] * df['last_xrate_quantity']) / df['nav'] * 100
    return df


def adjust_for_futures(portfolio: pd.DataFrame):
    futures = portfolio[portfolio['asset_class'] == 'FUTURE']

    if not futures.empty:
        sector_alloc = pd.read_excel('data.xlsx', sheet_name='Sector', header=4, index_col=0)
        sectors = sector_alloc.iloc[:, 3:]
        sectors.drop(["null", "NullGroup"], axis=1, inplace=True, errors="ignore")
        sectors = sectors.div(sectors.sum(axis=1), axis=0).mul(100)

        new_rows = []

        for _, future in futures.iterrows():
            underlying = future['underlying_name']
            exposure = future['Exposure']

            if underlying in sectors.index:
                sector_weights = sectors.loc[underlying] * exposure / 100

                for sector, weight in sector_weights.items():
                    new_row = {
                        'position_name': f"{underlying}_{sector}",
                        'asset_class': 'FUTURE',
                        'underlying_name': underlying,
                        'Exposure': weight,
                        'country': sector_alloc.loc[underlying, "Region"],
                        'Sector': sector
                    }
                    new_rows.append(new_row)

        if new_rows:
            new_rows_df = pd.DataFrame(new_rows)
            portfolio = pd.concat([portfolio, new_rows_df], ignore_index=True)

    return portfolio


def plot_combined_dataframe(benchmark: pd.DataFrame, portfolio: pd.DataFrame, name: str) -> (str, str):
    """
    Plot combined DataFrame for sector and country, showing benchmark weights, portfolio weights, and their differences.

    Args:
        benchmark (pd.DataFrame): Combined benchmark data.
        portfolio (pd.DataFrame): Portfolio data.
    """
    portfolio['Sector'] = portfolio['Sector'].str.extract(r'\d+\s*(.*)')[0]
    portfolio = adjust_for_futures(portfolio)

    benchmark_sector = group(benchmark, 'gics_sector_name', 'current_weight')
    portfolio_sector = group(portfolio, 'Sector', 'Exposure')

    combined_sector = pd.merge(benchmark_sector, portfolio_sector, left_index=True, right_index=True, how='outer',
                               suffixes=('_benchmark', '_portfolio')).fillna(0)
    combined_sector.columns = ['Benchmark', 'Portfolio']
    combined_sector['Difference'] = combined_sector['Portfolio'] - combined_sector['Benchmark']
    combined_sector.loc['Sum'] = combined_sector.sum()
    combined_sector.index.name = "Sector"

    sector = style_and_export_combined(combined_sector, name, "Sector")

    benchmark_region = benchmark.dropna(subset=['cntry_issue_iso'])
    benchmark_region['cntry_issue_iso'] = benchmark_region['cntry_issue_iso'].apply(
        lambda x: 'US' if x == 'US' else 'EU')
    benchmark_region = group(benchmark_region, 'cntry_issue_iso', 'current_weight')

    portfolio_region = portfolio.dropna(subset=['country'])
    portfolio_region['country'] = portfolio_region['country'].apply(
        lambda x: 'US' if x == 'US' else 'EU')
    portfolio_region = group(portfolio_region, 'country', 'Exposure')

    combined = pd.merge(benchmark_region, portfolio_region, left_index=True, right_index=True, how='outer',
                               suffixes=('_benchmark', '_portfolio')).fillna(0)
    combined.columns = ['Benchmark', 'Portfolio']
    combined['Difference'] = combined['Portfolio'] - combined['Benchmark']
    combined.loc['Sum'] = combined.sum()
    combined.index.name = "Region"

    region = style_and_export_combined(combined, name, "Region")

    return sector, region


def style_and_export_combined(df: pd.DataFrame, fund: str, kind: str) -> str:
    """
    Style the combined DataFrame and export it as an image.

    Args:
        df (pd.DataFrame): Combined DataFrame to be styled.
        fund (str): Kind of plot (Sector or Country).
    """
    bm_max_abs_value = max(abs(df['Benchmark'].iloc[:-1].min().min()),
                           abs(df['Benchmark'].iloc[:-1].max().max()))

    port_max_abs_value = max(abs(df['Portfolio'].iloc[:-1].min().min()),
                           abs(df['Portfolio'].iloc[:-1].max().max()))

    diff_max_abs_value = max(abs(df['Difference'].min().min()),
                             abs(df['Difference'].max().max()))

    cm = LinearSegmentedColormap.from_list("green", ["red", "white", "green"], N=len(df))

    styled = (
        df.style.bar(subset='Benchmark', cmap=cm, align=0, vmax=bm_max_abs_value, vmin=-bm_max_abs_value)
        .bar(subset='Portfolio', cmap=cm, align=0, vmax=port_max_abs_value, vmin=-port_max_abs_value)
        .bar(subset='Difference', cmap=cm, align=0, vmax=diff_max_abs_value, vmin=-diff_max_abs_value)
        .set_table_styles([
            {'selector': 'th.col0',
             'props': [('border-left', '1px solid black')]},
            {'selector': 'td.col0',
             'props': [('border-left', '1px solid black')]},
            {'selector': 'tr:last-child th, tr:last-child td',
             'props': [('border-top', '1px solid black')]}
        ])
        .format({
            'Benchmark': "{:.2f}%",
            'Portfolio': "{:.2f}%",
            'Difference': "{:.2f}%"
        }))
    path = f'output/allocation/{kind}_Exposure_{fund.replace("&", "_").replace(" ", "_")}.png'
    dfi.export(styled, path, table_conversion="selenium")
    return path


def generate_allocation_report():
    sxxp, spx, benchmark = get_benchmark_positions()

    name = "D&R Aktien"
    aktien = get_account_positions(id=mandate.get(name))

    aktien_sector, aktien_region = plot_combined_dataframe(benchmark, aktien, name)

    mail_data = {
        "D&R Aktien nach Sektoren": aktien_sector,
        "D&R Aktien nach Regionen": aktien_region
    }

    return mail_data