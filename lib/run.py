import os
import sys
import time
import traceback
import warnings

import lib.pool

SQL_QUERY = 'SELECT {columns} FROM {table} WHERE project_id = {project_id}'
SQL_INSERT = 'INSERT INTO {table}({columns}) VALUES ({placeholders})'
SQL_UPDATE = '''
    UPDATE {table}
    SET {placeholders}
    WHERE project_id = {project_id}
'''


class Run(object):
    def __init__(self, repo_root, attributes, database, threshold, processes):
        self.repo_root = repo_root
        self.attributes = attributes
        self.database = database
        self.threshold = threshold
        self.processes = processes

    def run(self, samples, table):
        try:
            sys.stdout.write('{0}\n'.format('#' * 25))
            sys.stdout.write('{0}\n'.format(str.center('Run', 25)))
            sys.stdout.write('{0}\n'.format('#' * 25))
            self.attributes.global_init(samples)
            with lib.pool.NonDaemonicProcessPool(self.processes) as pool:
                pool.starmap(
                    self._process,
                    [(project_id, table) for project_id in samples],
                    chunksize=1
                )
            sys.stdout.write('{0}\n'.format('#' * 25))
        except Exception as e:
            extype, exvalue, extrace = sys.exc_info()
            traceback.print_exception(extype, exvalue, extrace)

    def _process(self, project_id, table):
        try:
            rresults = self.attributes.run(project_id, self.repo_root)
        except:
            sys.stderr.write('Exception\n\n')
            sys.stderr.write('  Project ID   {0}\n'.format(project_id))
            extype, exvalue, extrace = sys.exc_info()
            traceback.print_exception(extype, exvalue, extrace)
        finally:
            if rresults is not None:
                self._save(project_id, rresults, table)

        # HACK: Waiting for mysqld to reclaim its connection
        time.sleep(0.5)

    def _save(self, project_id, rresults, table):
        if self.attributes.is_persistence_enabled:
            # Merge raw results from current run with existing ones (if any)
            _rresults = self._get(project_id, table)
            if _rresults is None:
                return
            is_existing = True if _rresults else False
            _rresults.update(rresults)
            score = self.attributes.score(_rresults)
            self._print_outcome(project_id, score)

            columns = ('project_id', 'score')
            values = (project_id, score)
            for key in rresults:
                if self.attributes.get(key).persist:
                    if rresults[key] is not None:
                        columns += (key,)
                        values += (rresults[key],)

            if is_existing:
                # Update
                query = SQL_UPDATE.format(
                    project_id=project_id, table=table,
                    placeholders=('=%s,'.join(columns) + '=%s')
                )
            else:
                # Insert
                query = SQL_INSERT.format(
                    columns=','.join(columns), table=table,
                    placeholders=','.join(['%s' for i in range(len(columns))])
                )

            try:
                self.database.connect()
                self.database.post(query, values)
            finally:
                self.database.disconnect()
        else:
            if 'DEBUG' in os.environ:
                for (attribute, result) in rresults.items():
                    print('[{0:10d}] {1:25s} {2}'.format(
                        project_id, attribute, result
                    ))

    def _get(self, project_id, table):
        rresults = None

        try:
            columns = [
                attribute.name for attribute in self.attributes.attributes
            ]

            self.database.connect()
            output = self.database.get(
                SQL_QUERY.format(
                    columns=','.join(columns), table=table,
                    project_id=project_id
                )
            )
            if output is not None:
                _rresults = dict()
                for (index, column) in enumerate(columns):
                    _rresults[column] = output[index]

                # Use the raw results from the database iff at least one
                # attribute has a non-NULL value. Typically, a project that was
                # non active at the time reaper was run will have the value for
                # all attributes as NULL in the database.
                for (_, value) in _rresults.items():
                    if value:
                        rresults = _rresults
                        break
        finally:
            self.database.disconnect()

        return rresults

    def _print_outcome(self, project_id, score):
        # Generate a green checkmark or red x using terminal escapes
        cresult = '\033[92m✓\033[0m'
        if score < self.threshold:
            cresult = '\033[91m✘\033[0m'

        sys.stdout.write(
            ' [{0:>10d}] {1} {2}\n'.format(project_id, score, cresult)
        )
