import sys
import numpy as np
import pandas as pd
from typing import Any, Callable, Dict, List, Tuple, Union
from vantage6.algorithm.client import AlgorithmClient
from vantage6.algorithm.tools.util import info, error
from vantage6.algorithm.tools.decorators import algorithm_client, data


MINIMUM_ORGANIZATIONS = 3


@algorithm_client
def master(
    client: AlgorithmClient,
    time_column_name: str,
    censor_column_name: str,
    bin_size: int = None,
    query_string: str = None,
    organization_ids: List[int] = None
) -> Dict[str, Union[str, List[str]]]:
    """Compute Kaplan-Meier curve in a federated environment.

    Parameters:
    - client: Vantage6 client object
    - time_column_name: Name of the column representing time
    - censor_column_name: Name of the column representing censoring
    - binning: Simple KM or use binning to obfuscate events
    - organization_ids: List of organization IDs to include (default: None, includes all)

    Returns:
    - Dictionary containing Kaplan-Meier curve and local event tables
    """
    info('Collecting information on participating organizations')
    if not isinstance(organization_ids, list):
        organizations = client.organization.list()
        ids = [organization.get("id") for organization in organizations]
    else:
        ids = organization_ids

    # if len(ids) < MINIMUM_ORGANIZATIONS:
    #     error(f"To further ensure privacy, a minimum of {MINIMUM_ORGANIZATIONS} participating organizations is required")
    #     sys.exit(1)

    info(f'Sending task to organizations {ids}')
    km, local_event_tables = calculate_km(
        client=client,
        ids=ids,
        time_column_name=time_column_name,
        censor_column_name=censor_column_name,
        bin_size=bin_size,
        query_string=query_string
    )
    return {'kaplanMeier': km.to_json(), 'local_event_tables': [t.to_json() for t in local_event_tables]}


def calculate_km(
    client: AlgorithmClient,
    ids: List[int],
    time_column_name: str,
    censor_column_name: str,
    bin_size: int = None,
    query_string: str = None
) -> Tuple[pd.DataFrame, List[pd.DataFrame]]:
    """Calculate Kaplan-Meier curve and local event tables.

    Parameters:
    - client: Vantage6 client object
    - ids: List of organization IDs
    - time_column_name: Name of the column representing time
    - censor_column_name: Name of the column representing censoring
    - binning: Simple KM or use binning to obfuscate events

    Returns:
    - Tuple containing Kaplan-Meier curve (DataFrame) and local event tables (list of DataFrames)
    """
    info('Collecting unique event times')
    kwargs_dict = dict(
        time_column_name=time_column_name,
        query_string=query_string)
    method = 'get_unique_event_times'
    local_unique_event_times_aggregated = launch_subtask(client, method, kwargs_dict, ids)
    unique_event_times = {0}
    for local_unique_event_times in local_unique_event_times_aggregated:
        unique_event_times |= set(local_unique_event_times)
    info(f'Collected unique event times for {len(local_unique_event_times_aggregated)} organization(s)')

    # Apply binning to obfuscate event times
    if bin_size:
        info('Binning unique times')
        unique_event_times = np.arange(
                0, int(np.max(list(unique_event_times))) + bin_size, bin_size
            )

    info('Collecting local event tables')
    kwargs_dict = dict(
        time_column_name=time_column_name,
        unique_event_times=list(unique_event_times),
        censor_column_name=censor_column_name,
        bin_size=bin_size,
        query_string=query_string)
    method = 'get_km_event_table'
    local_event_tables = launch_subtask(client, method, kwargs_dict, ids)
    local_event_tables = [pd.read_json(event_table) for event_table in local_event_tables]
    info(f'Collected local event tables for {len(local_event_tables)} organization(s)')

    info('Aggregating event tables')
    km = pd.concat(local_event_tables).groupby(time_column_name, as_index=False).sum()
    km['hazard'] = km['deaths'] / km['at_risk']
    km['survival_cdf'] = (1 - km['hazard']).cumprod()
    info('Kaplan-Meier curve has been computed successfully')
    return km, local_event_tables


@data(1)
def get_unique_event_times(df: pd.DataFrame, *args, **kwargs) -> List[str]:
    """Get unique event times from a DataFrame.

    Parameters:
    - df: Input DataFrame
    - kwargs: Additional keyword arguments, including time_column_name and query_string

    Returns:
    - List of unique event times
    """
    time_column_name = kwargs.get("time_column_name")
    query_string = kwargs.get("query_string")
    return (
        df
        .query(query_string)[time_column_name]
        .unique()
        .tolist()
        )


@data(1)
def get_km_event_table(df: pd.DataFrame, *args, **kwargs) -> str:
    """Calculate death counts, total counts, and at-risk counts at each unique event time.

    Parameters:
    - df: Input DataFrame
    - kwargs: Additional keyword arguments, including time_column_name, unique_event_times, and censor_column_name

    Returns:
    - JSON-formatted string representing the calculated event table
    """

    # parse kwargs
    time_column_name = kwargs.get("time_column_name")
    unique_event_times = kwargs.get("unique_event_times")
    censor_column_name = kwargs.get("censor_column_name")
    bin_size = kwargs.get("bin_size", None)
    query_string = kwargs.get("query_string", None)

    # Filter the local dataframe with the query
    info(f"Overall number of patients: {df.shape[0]}")
    df = df.query(query_string)
    info(f"Number of patients in the cohort: {df.shape[0]}")

    # Apply binning to obfuscate event times
    if bin_size:
        # Convert time_column to appropriate data type for binning if needed
        if not pd.api.types.is_numeric_dtype(df[time_column_name]):
            df[time_column_name] = pd.to_numeric(df[time_column_name], errors='coerce')

        # Bin event time data
        info('Binning event times to compute tables')
        df[time_column_name] = np.float64(pd.cut(
            df[time_column_name], bins=unique_event_times,
            labels=unique_event_times[1:]
        ))

    # Group by the time column, aggregating both death and total counts simultaneously
    km_df = (
        df
        .groupby(time_column_name)
        .agg(deaths=(censor_column_name, 'sum'), total=(censor_column_name, 'count'))
        .reset_index()
        )

    # Calculate "at-risk" counts at each unique event time
    km_df['at_risk'] = km_df['total'].iloc[::-1].cumsum().iloc[::-1]

    # Convert DataFrame to JSON
    return km_df.to_json()


def launch_subtask(
    client: AlgorithmClient,
    method: Callable[[Any], Any],
    ids: List[int],
    **kwargs
) -> List[Dict[str, Union[str, List[str]]]]:
    """Launches a subtask to multiple organizations and waits for results.

    Parameters:
    - client: The Vantage6 client object used for communication with the server.
    - method: The callable method/function to be executed as a subtask by the organizations.
    - ids: A list of organization IDs to which the subtask will be distributed.
    - **kwargs: Additional keyword arguments to be passed to the method/function.

    Returns:
    - A list of dictionaries containing results obtained from the organizations.
    """
    info(f'Sending task to organizations {ids}')
    task = client.task.create(
        input_={
            'method': method,
            'kwargs': kwargs
        },
        organizations=ids
    )

    info("Waiting for results")
    results = client.wait_for_results(task_id=task.get("id"), interval=1)
    info(f"Results obtained for {method}!")
    return results
