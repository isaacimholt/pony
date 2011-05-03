# for ORM core:
import re, sys, threading
from operator import attrgetter, itemgetter
from itertools import count, ifilter, ifilterfalse, izip

# for SQL translator:
import __builtin__, types, inspect
from types import NoneType
from compiler import ast
from decimal import Decimal
from datetime import date, datetime

# for ORM core:

try: from pony.thirdparty import etree
except ImportError: etree = None

from pony import options, sqlbuilding
from pony.clobtypes import LongStr, LongUnicode
from pony.sqlsymbols import *
from pony.utils import (
    localbase, simple_decorator, decorator_with_params,
    import_module, parse_expr, is_ident, reraise
    )

# for SQL translator:

from pony.decompiling import decompile
from pony.clobtypes import LongStr, LongUnicode
from pony.sqlbuilding import SQLBuilder
from pony.sqlsymbols import *

__all__ = '''
    DBException RowNotFound MultipleRowsFound TooManyRowsFound

    Warning Error InterfaceError DatabaseError DataError OperationalError
    IntegrityError InternalError ProgrammingError NotSupportedError

    OrmError DiagramError SchemaError MappingError ConstraintError IndexError ObjectNotFound
    MultipleObjectsFoundError TooManyObjectsFoundError OperationWithDeletedObjectError
    TransactionError TransactionIntegrityError IsolationError CommitException RollbackException
    UnrepeatableReadError UnresolvableCyclicDependency UnexpectedError

    Database sql_debug

    Entity Diagram Optional Required Unique PrimaryKey Set
    flush commit rollback with_transaction

    LongStr LongUnicode

    TranslationError select exists   
    '''.split()

debug = True

def sql_debug(value):
    global debug
    debug = value

class DBException(Exception):
    def __init__(exc, *args, **keyargs):
        exceptions = keyargs.pop('exceptions', [])
        assert not keyargs
        if not args and exceptions:
            if len(exceptions) == 1: args = getattr(exceptions[0], 'args', ())
            else: args = ('Multiple exceptions have occured',)
        Exception.__init__(exc, *args)
        exc.exceptions = exceptions

class RowNotFound(DBException): pass
class MultipleRowsFound(DBException): pass
class TooManyRowsFound(DBException): pass

##StandardError
##        |__Warning
##        |__Error
##           |__InterfaceError
##           |__DatabaseError
##              |__DataError
##              |__OperationalError
##              |__IntegrityError
##              |__InternalError
##              |__ProgrammingError
##              |__NotSupportedError

class Warning(DBException): pass
class Error(DBException): pass
class   InterfaceError(Error): pass
class   DatabaseError(Error): pass
class     DataError(DatabaseError): pass
class     OperationalError(DatabaseError): pass
class     IntegrityError(DatabaseError): pass
class     InternalError(DatabaseError): pass
class     ProgrammingError(DatabaseError): pass
class     NotSupportedError(DatabaseError): pass

def wrap_dbapi_exceptions(provider, func, *args, **keyargs):
    try: return func(*args, **keyargs)
    except provider.NotSupportedError, e: raise NotSupportedError(exceptions=[e])
    except provider.ProgrammingError, e: raise ProgrammingError(exceptions=[e])
    except provider.InternalError, e: raise InternalError(exceptions=[e])
    except provider.IntegrityError, e: raise IntegrityError(exceptions=[e])
    except provider.OperationalError, e: raise OperationalError(exceptions=[e])
    except provider.DataError, e: raise DataError(exceptions=[e])
    except provider.DatabaseError, e: raise DatabaseError(exceptions=[e])
    except provider.InterfaceError, e:
        if e.args == (0, '') and getattr(provider, '__name__', None) == 'pony.dbproviders.mysql':
            raise InterfaceError('MySQL server misconfiguration', exceptions=[e])
        raise InterfaceError(exceptions=[e])
    except provider.Error, e: raise Error(exceptions=[e])
    except provider.Warning, e: raise Warning(exceptions=[e])

class OrmError(Exception): pass

class DiagramError(OrmError): pass
class SchemaError(OrmError): pass
class MappingError(OrmError): pass
class ConstraintError(OrmError): pass
class IndexError(OrmError): pass
class ObjectNotFound(OrmError):
    def __init__(exc, entity, pkval):
        if type(pkval) is tuple:
            msg = '%s%r' % (entity.__name__, pkval)
        else: msg = '%s(%r)' % (entity.__name__, pkval)
        OrmError.__init__(exc, msg)
        exc.entity = entity
        exc.pkval = pkval

class MultipleObjectsFoundError(OrmError): pass
class TooManyObjectsFoundError(OrmError): pass
class OperationWithDeletedObjectError(OrmError): pass
class TransactionError(OrmError): pass
class TransactionIntegrityError(TransactionError): pass
class IsolationError(TransactionError): pass
class CommitException(TransactionError):
    def __init__(exc, msg, exceptions):
        Exception.__init__(exc, msg)
        exc.exceptions = exceptions
class PartialCommitException(TransactionError):
    def __init__(exc, msg, exceptions):
        Exception.__init__(exc, msg)
        exc.exceptions = exceptions
class RollbackException(TransactionError): pass
class UnrepeatableReadError(IsolationError): pass
class UnresolvableCyclicDependency(TransactionError): pass
class UnexpectedError(TransactionError): pass


class TranslationError(Exception): pass

###############################################################################

sql_cache = {}

def adapt_sql(sql, paramstyle):
    result = sql_cache.get((sql, paramstyle))
    if result is not None: return result
    pos = 0
    result = []
    args = []
    keyargs = {}
    if paramstyle in ('format', 'pyformat'): sql = sql.replace('%', '%%')
    while True:
        try: i = sql.index('$', pos)
        except ValueError:
            result.append(sql[pos:])
            break
        result.append(sql[pos:i])
        if sql[i+1] == '$':
            result.append('$')
            pos = i+2
        else:
            try: expr, _ = parse_expr(sql, i+1)
            except ValueError:
                raise # TODO
            pos = i+1 + len(expr)
            if expr.endswith(';'): expr = expr[:-1]
            compile(expr, '<?>', 'eval')  # expr correction check
            if paramstyle == 'qmark':
                args.append(expr)
                result.append('?')
            elif paramstyle == 'format':
                args.append(expr)
                result.append('%s')
            elif paramstyle == 'numeric':
                args.append(expr)
                result.append(':%d' % len(args))
            elif paramstyle == 'named':
                key = 'p%d' % (len(keyargs) + 1)
                keyargs[key] = expr
                result.append(':' + key)
            elif paramstyle == 'pyformat':
                key = 'p%d' % (len(keyargs) + 1)
                keyargs[key] = expr
                result.append('%%(%s)s' % key)
            else: raise NotImplementedError
    adapted_sql = ''.join(result)
    if args:
        source = '(%s,)' % ', '.join(args)
        code = compile(source, '<?>', 'eval')
    elif keyargs:
        source = '{%s}' % ','.join('%r:%s' % item for item in keyargs.items())
        code = compile(source, '<?>', 'eval')
    else:
        code = compile('None', '<?>', 'eval')
        if paramstyle in ('format', 'pyformat'): sql = sql.replace('%%', '%')
    result = adapted_sql, code
    sql_cache[(sql, paramstyle)] = result
    return result

next_num = count().next

class Local(localbase):
    def __init__(local):
        local.db2cache = {}

local = Local()        

select_re = re.compile(r'\s*select\b', re.IGNORECASE)

class Database(object):
    def __init__(database, provider, *args, **keyargs):
        if isinstance(provider, basestring): provider = import_module('pony.dbproviders.' + provider)
        database.provider = provider
        database.args = args
        database.keyargs = keyargs
        database._pool = provider.get_pool(*args, **keyargs)
        database.priority = 0
        database.optimistic = True
        database._insert_cache = {}
        # connection test with imediate release:
        connection = wrap_dbapi_exceptions(database.provider, database._pool.connect)
        wrap_dbapi_exceptions(provider, database._pool.release, connection)
    def get_connection(database):
        cache = database._get_cache()
        cache.optimistic = False
        return cache.connection
    def _get_cache(database):
        cache = local.db2cache.get(database)
        if cache is not None: return cache
        connection = wrap_dbapi_exceptions(database.provider, database._pool.connect)
        cache = local.db2cache[database] = Cache(database, connection)
        return cache
    def flush(database):
        cache = database._get_cache()
        cache.flush()
    def commit(database):
        cache = local.db2cache.get(database)
        if cache is not None: cache.commit()
    def rollback(database):
        cache = local.db2cache.get(database)
        if cache is not None: cache.rollback()
    def execute(database, sql, globals=None, locals=None):
        sql = sql[:]  # sql = templating.plainstr(sql)
        if globals is None:
            assert locals is None
            globals = sys._getframe(1).f_globals
            locals = sys._getframe(1).f_locals
        provider = database.provider
        adapted_sql, code = adapt_sql(sql, provider.paramstyle)
        values = eval(code, globals, locals)
        cache = database._get_cache()
        cache.optimistic = False
        cursor = cache.connection.cursor()
        if values is None: wrap_dbapi_exceptions(provider, cursor.execute, adapted_sql)
        else: wrap_dbapi_exceptions(provider, cursor.execute, adapted_sql, values)
        return cursor
    def select(database, sql, globals=None, locals=None):
        sql = sql[:]  # sql = templating.plainstr(sql)
        if not select_re.match(sql): sql = 'select ' + sql
        if globals is None:
            assert locals is None
            globals = sys._getframe(1).f_globals
            locals = sys._getframe(1).f_locals
        provider = database.provider
        adapted_sql, code = adapt_sql(sql, provider.paramstyle)
        values = eval(code, globals, locals)
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        if values is None: wrap_dbapi_exceptions(provider, cursor.execute, adapted_sql)
        else: wrap_dbapi_exceptions(provider, cursor.execute, adapted_sql, values)
        result = cursor.fetchmany(options.MAX_ROWS_COUNT)
        if cursor.fetchone() is not None: raise TooManyRowsFound
        if len(cursor.description) == 1: result = [ row[0] for row in result ]
        else:
            row_class = type("row", (tuple,), {})
            for i, column_info in enumerate(cursor.description):
                column_name = column_info[0]
                if not is_ident(column_name): continue
                if hasattr(tuple, column_name) and column_name.startswith('__'): continue
                setattr(row_class, column_name, property(itemgetter(i)))
            result = [ row_class(row) for row in result ]
        return result
    def get(database, sql, globals=None, locals=None):
        if globals is None:
            assert locals is None
            globals = sys._getframe(1).f_globals
            locals = sys._getframe(1).f_locals
        rows = database.select(sql, globals, locals)
        if not rows: raise RowNotFound
        if len(rows) > 1: raise MultipleRowsFound
        row = rows[0]
        return row
    def exists(database, sql, globals=None, locals=None):
        sql = sql[:]  # sql = templating.plainstr(sql)
        if not select_re.match(sql): sql = 'select ' + sql
        if globals is None:
            assert locals is None
            globals = sys._getframe(1).f_globals
            locals = sys._getframe(1).f_locals
        provider = database.provider
        adapted_sql, code = adapt_sql(sql, provider.paramstyle)
        values = eval(code, globals, locals)
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        if values is None: wrap_dbapi_exceptions(provider, cursor.execute, adapted_sql)
        else: wrap_dbapi_exceptions(provider, cursor.execute, adapted_sql, values)
        result = cursor.fetchone()
        return bool(result)
    def insert(database, table_name, **keyargs):
        table_name = table_name[:]  # table_name = templating.plainstr(table_name)
        cache = database._get_cache()
        cache.optimistic = False
        query_key = (table_name,) + tuple(keyargs)  # keys are not sorted deliberately!!
        cached_sql = database._insert_cache.get(query_key)
        if cached_sql is None:
            ast = [ INSERT, table_name, keyargs.keys(), [ [PARAM, i] for i in range(len(keyargs)) ] ]
            sql, adapter = database._ast2sql(ast)
            cached_sql = sql, adapter
            database._insert_cache[query_key] = cached_sql
        else: sql, adapter = cached_sql
        arguments = adapter(keyargs.values())  # order of values same as order of keys
        cursor = database._exec_sql(sql, arguments)
        return getattr(cursor, 'lastrowid', None)
    def _ast2sql(database, sql_ast):
        cache = database._get_cache()
        sql, adapter = database.provider.ast2sql(cache.connection, sql_ast)
        return sql, adapter
    def _exec_sql(database, sql, arguments=None):
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        if debug:
            print sql
            print arguments
            print
        provider = database.provider
        if arguments is None: wrap_dbapi_exceptions(provider, cursor.execute, sql)
        else: wrap_dbapi_exceptions(provider, cursor.execute, sql, arguments)
        return cursor
    def _exec_sql_many(database, sql, arguments_list=None):
        cache = database._get_cache()
        cache.optimistic = False
        cursor = cache.connection.cursor()
        if debug:
            print 'EXECUTEMANY', sql
            print arguments_list
            print
        provider = database.provider
        if arguments_list is None: wrap_dbapi_exceptions(provider, cursor.executemany, sql)
        else: wrap_dbapi_exceptions(provider, cursor.executemany, sql, arguments_list)
        return cursor
    def _commit_commands(database, commands):
        cache = database._get_cache()
        assert not cache.has_anything_to_save()
        cursor = cache.connection.cursor()
        provider = database.provider
        for command in commands:
            if debug: print 'DDLCOMMAND', command
            wrap_dbapi_exceptions(provider, cursor.execute, command)
        if debug: print 'COMMIT'
        wrap_dbapi_exceptions(provider, cache.connection.commit)
    def generate_mapping(database, *args, **keyargs):
        outer_dict = sys._getframe(1).f_locals
        diagram = outer_dict.get('_diagram_')
        if diagram is None: raise MappingError('No default diagram found')
        diagram.generate_mapping(database, *args, **keyargs)

###############################################################################

class NotLoadedValueType(object):
    def __repr__(self): return 'NOT_LOADED'

NOT_LOADED = NotLoadedValueType()

class DefaultValueType(object):
    def __repr__(self): return 'DEFAULT'

DEFAULT = DefaultValueType()

class NoUndoNeededValueType(object):
    def __repr__(self): return 'NO_UNDO_NEEDED'

NO_UNDO_NEEDED = NoUndoNeededValueType()

class DescWrapper(object):
    def __init__(self, attr):
        self.attr = attr
    def __repr__(self):
        return '<DescWrapper(%s)>' % self.attr

next_attr_id = count(1).next

class Attribute(object):
    __slots__ = 'is_required', 'is_unique', 'is_indexed', 'is_pk', 'is_collection', 'is_ref', 'is_basic', \
                'id', 'pk_offset', 'py_type', 'sql_type', 'entity', 'name', \
                'args', 'auto', 'default', 'reverse', 'composite_keys', \
                'column', 'columns', 'col_paths', '_columns_checked', 'converters', 'keyargs'
    def __init__(attr, py_type, *args, **keyargs):
        if attr.__class__ is Attribute: raise TypeError("'Attribute' is abstract type")
        attr.is_required = isinstance(attr, Required)
        attr.is_unique = isinstance(attr, Unique)  # Also can be set to True later
        attr.is_indexed = attr.is_unique  # Also can be set to True later
        attr.is_pk = isinstance(attr, PrimaryKey)
        if attr.is_pk: attr.pk_offset = 0
        else: attr.pk_offset = None
        attr.id = next_attr_id()
        if not isinstance(py_type, basestring) and not isinstance(py_type, type):
            raise TypeError('Incorrect type of attribute: %r' % py_type)
        if py_type == 'Entity' or py_type is Entity:
            raise TypeError('Cannot link attribute to Entity class. Must use Entity subclass instead')
        attr.py_type = py_type
        attr.is_collection = isinstance(attr, Collection)
        attr.is_ref = not attr.is_collection and isinstance(attr.py_type, (EntityMeta, basestring))
        attr.is_basic = not attr.is_collection and not attr.is_ref
        attr.sql_type = keyargs.pop('sql_type', None)
        attr.entity = attr.name = None
        attr.args = args
        attr.auto = keyargs.pop('auto', False)

        try: attr.default = keyargs.pop('default')
        except KeyError: attr.default = None
        else:
            if attr.default is None and attr.is_required:
                raise TypeError('Default value for required attribute cannot be None' % attr)

        attr.reverse = keyargs.pop('reverse', None)
        if not attr.reverse: pass
        elif not isinstance(attr.reverse, (basestring, Attribute)):
            raise TypeError("Value of 'reverse' option must be name of reverse attribute). Got: %r" % attr.reverse)
        elif not isinstance(attr.py_type, (basestring, EntityMeta)):
            raise TypeError('Reverse option cannot be set for this type: %r' % attr.py_type)

        attr.column = keyargs.pop('column', None)
        attr.columns = keyargs.pop('columns', None)
        if attr.column is not None:
            if attr.columns is not None:
                raise TypeError("Parameters 'column' and 'columns' cannot be specified simultaneously")
            if not isinstance(attr.column, basestring):
                raise TypeError("Parameter 'column' must be a string. Got: %r" % attr.column)
            attr.columns = [ attr.column ]
        elif attr.columns is not None:
            if not isinstance(attr.columns, (tuple, list)):
                raise TypeError("Parameter 'columns' must be a list. Got: %r'" % attr.columns)
            if not attr.columns: raise TypeError("Parameter 'columns' must not be empty list")
            for column in attr.columns:
                if not isinstance(column, basestring):
                    raise TypeError("Items of parameter 'columns' must be strings. Got: %r" % attr.columns)
            if len(attr.columns) == 1: attr.column = attr.columns[0]
        else: attr.columns = []
        attr.col_paths = []
        attr._columns_checked = False
        attr.composite_keys = []
        attr.keyargs = keyargs
        attr.converters = []
    def _init_(attr, entity, name):
        attr.entity = entity
        attr.name = name
    def __repr__(attr):
        owner_name = not attr.entity and '?' or attr.entity.__name__
        return '%s.%s' % (owner_name, attr.name or '?')
    def check(attr, val, obj=None, entity=None, from_db=False):
        assert val is not NOT_LOADED
        if entity is not None: pass
        elif obj is not None: entity = obj.__class__
        else: entity = attr.entity
        if val is DEFAULT:
            default = attr.default
            if default is None:
                if attr.is_required and not attr.auto: raise ConstraintError(
                    'Required attribute %s.%s does not specified' % (entity.__name__, attr.name))
                return None
            if callable(default): val = default()
            else: val = default
        elif val is None:
            if attr.is_required:
                if obj is None: raise ConstraintError(
                    'Required attribute %s.%s cannot be set to None' % (entity.__name__, attr.name))
                else: raise ConstraintError(
                    'Required attribute %s.%s for %r cannot be set to None' % (entity.__name__, attr.name, obj))
            return val
        reverse = attr.reverse
        if not reverse:
            if isinstance(val, attr.py_type): return val
            elif isinstance(val, Entity):
                raise TypeError('Attribute %s.%s must be of %s type. Got: %s'
                                % (attr.entity.__name__, attr.name, attr.py_type.__name__, val))
            if attr.converters:
                assert len(attr.converters) == 1
                converter = attr.converters[0]
                if converter is not None:
                    if from_db: return converter.sql2py(val)
                    else: return converter.validate(val)
            return attr.py_type(val)
        if not isinstance(val, reverse.entity):
            raise ConstraintError('Value of attribute %s.%s must be an instance of %s. Got: %s'
                                  % (entity.__name__, attr.name, reverse.entity.__name__, val))
        if obj is not None: cache = obj._cache_
        else: cache = entity._get_cache_()
        if cache is not val._cache_:
            raise TransactionError('An attempt to mix objects belongs to different caches')
        return val
    def load(attr, obj):
        if not attr.columns:
            reverse = attr.reverse
            assert reverse is not None and reverse.columns
            objects = reverse.entity._find_in_db_({reverse : obj}, 1)
            assert len(objects) == 1
            return objects[0]
        obj._load_()
        return obj._curr_[attr.name]
    def __get__(attr, obj, cls=None):
        if obj is None: return attr
        result = attr.get(obj)
        if attr.pk_offset is not None: return result
        bit = obj._bits_[attr]
        wbits = obj._wbits_
        if wbits is not None and not wbits & bit: obj._rbits_ |= bit
        return result
    def get(attr, obj):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        val = obj._curr_.get(attr.name, NOT_LOADED)
        if val is NOT_LOADED: val = attr.load(obj)
        return val
    def __set__(attr, obj, val, undo_funcs=None):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        is_reverse_call = undo_funcs is not None
        reverse = attr.reverse
        val = attr.check(val, obj, from_db=False)
        pkval = obj._pkval_
        if attr.pk_offset is not None:
            if pkval is None: pass
            elif obj._pk_is_composite_:
                if val == pkval[attr.pk_offset]: return
            elif val == pkval: return
            raise TypeError('Cannot change value of primary key')
        curr =  obj._curr_.get(attr.name, NOT_LOADED)
        if curr is NOT_LOADED and reverse and not reverse.is_collection:
            assert not is_reverse_call
            curr = attr.load(obj)
        cache = obj._cache_
        status = obj._status_
        wbits = obj._wbits_
        if wbits is not None:
            obj._wbits_ = wbits | obj._bits_[attr]
            if status != 'updated':
                if status in ('loaded', 'saved'): cache.to_be_checked.append(obj)
                else: assert status == 'locked'
                obj._status_ = 'updated'
                cache.updated.add(obj)
        if not attr.reverse and not attr.is_indexed:
            obj._curr_[attr.name] = val
            return
        if not is_reverse_call: undo_funcs = []
        undo = []
        def undo_func():
            obj._status_ = status
            obj._wbits_ = wbits
            if wbits == 0: cache.updated.remove(obj)
            if status in ('loaded', 'saved'):
                to_be_checked = cache.to_be_checked
                if to_be_checked and to_be_checked[-1] is obj: to_be_checked.pop()
                assert obj not in to_be_checked
            obj._curr_[attr.name] = curr
            for index, old_key, new_key in undo:
                if new_key is NO_UNDO_NEEDED: pass
                else: del index[new_key]
                if old_key is NO_UNDO_NEEDED: pass
                else: index[old_key] = obj
        undo_funcs.append(undo_func)
        if curr == val: return
        try:
            if attr.is_unique:
                cache.update_simple_index(obj, attr, curr, val, undo)
            for attrs, i in attr.composite_keys:
                get = obj._curr_.get
                vals = [ get(a.name, NOT_LOADED) for a in attrs ]
                currents = tuple(vals)
                vals[i] = val
                vals = tuple(vals)
                cache.update_composite_index(obj, attrs, currents, vals, undo)

            obj._curr_[attr.name] = val
                
            if not reverse: pass
            elif not is_reverse_call: attr.update_reverse(obj, curr, val, undo_funcs)
            elif curr is not None:
                if not reverse.is_collection:
                    assert curr is not NOT_LOADED
                    reverse.__set__(curr, None, undo_funcs)
                elif isinstance(reverse, Set):
                    if curr is NOT_LOADED: pass
                    else: reverse.reverse_remove((curr,), obj, undo_funcs)
                else: raise NotImplementedError
        except:
            if not is_reverse_call:
                for undo_func in reversed(undo_funcs): undo_func()
            raise
    def db_set(attr, obj, prev, is_reverse_call=False):
        assert obj._status_ not in ('created', 'deleted', 'cancelled')
        assert attr.pk_offset is None
        reverse = attr.reverse
        get_curr = obj._curr_.get
        prev = attr.check(prev, obj, from_db=True)
        old_prev = obj._prev_.get(attr.name, NOT_LOADED)
        if old_prev == prev: return
        bit = obj._bits_[attr]
        if obj._rbits_ & bit:
            assert old_prev is not NOT_LOADED
            raise UnrepeatableReadError('Value of %s.%s for %s was updated outside of current transaction (was: %s, now: %s)'
                                        % (obj.__class__.__name__, attr.name, obj, old_prev, prev))
        obj._prev_[attr.name] = prev
        if obj._wbits_ & bit: return
        val = prev
        curr = get_curr(attr.name, NOT_LOADED)
        assert curr == old_prev

        if not attr.reverse and not attr.is_indexed: return
        cache = obj._cache_
        if attr.is_unique: cache.db_update_simple_index(obj, attr, curr, val)
        for attrs, i in attr.composite_keys:
            vals = [ get_curr(a.name, NOT_LOADED) for a in attrs ]
            currents = tuple(vals)
            vals[i] = val
            vals = tuple(vals)
            cache.db_update_composite_index(obj, attrs, currents, vals)
        if not reverse: pass
        elif not is_reverse_call: attr.db_update_reverse(obj, curr, val)
        elif curr is not None:
            if not reverse.is_collection:
                assert curr is not NOT_LOADED
                reverse.db_set(curr, None, is_reverse_call=True)
            elif isinstance(reverse, Set):
                if curr is NOT_LOADED: pass
                else: reverse.db_reverse_remove((curr,), obj)
            else: raise NotImplementedError
        obj._curr_[attr.name] = val
    def update_reverse(attr, obj, curr, val, undo_funcs):
        reverse = attr.reverse
        if not reverse.is_collection:
            if curr is NOT_LOADED: pass
            elif curr is not None: reverse.__set__(curr, None, undo_funcs)
            if val is not None: reverse.__set__(val, obj, undo_funcs)
        elif isinstance(reverse, Set):
            if curr is NOT_LOADED: pass
            elif curr is not None: reverse.reverse_remove((curr,), obj, undo_funcs)
            if val is not None: reverse.reverse_add((val,), obj, undo_funcs)
        else: raise NotImplementedError
    def db_update_reverse(attr, obj, curr, val):
        reverse = attr.reverse
        if not reverse.is_collection:
            if curr is NOT_LOADED: pass
            elif curr is not None: reverse.db_set(curr, None)
            if val is not None: reverse.db_set(val, obj)
        elif isinstance(reverse, Set):
            if curr is NOT_LOADED: pass
            elif curr is not None: reverse.db_reverse_remove((curr,), obj)
            if val is not None: reverse.db_reverse_add((val,), obj)
        else: raise NotImplementedError
    def __delete__(attr, obj):
        raise NotImplementedError
    def get_raw_values(attr, val):
        reverse = attr.reverse
        if not reverse: return (val,)
        rentity = reverse.entity
        if val is None: return rentity._pk_nones_
        return val._get_raw_pkval_()
    def get_columns(attr):
        assert not attr.is_collection
        assert not isinstance(attr.py_type, basestring)
        if attr._columns_checked: return attr.columns

        provider = attr.entity._diagram_.database.provider
        reverse = attr.reverse
        if not reverse: # attr is not part of relationship
            if not attr.columns: attr.columns = [ attr.name ]
            elif len(attr.columns) > 1: raise MappingError("Too many columns were specified for %s" % attr)
            attr.col_paths = [ attr.name ]
            attr.converters = [ provider.get_converter_by_attr(attr) ]
        else:
            def generate_columns():
                reverse_pk_columns = reverse.entity._get_pk_columns_()
                reverse_pk_col_paths = reverse.entity._pk_paths_
                if not attr.columns:
                    if len(reverse_pk_columns) == 1: attr.columns = [ attr.name ]
                    else:
                        prefix = attr.name + '_'
                        attr.columns = [ prefix + column for column in reverse_pk_columns ]
                elif len(attr.columns) != len(reverse_pk_columns): raise MappingError(
                    'Invalid number of columns specified for %s' % attr)
                attr.col_paths = [ '-'.join((attr.name, paths)) for paths in reverse_pk_col_paths ]
                attr.converters = []
                for a in reverse.entity._pk_attrs_:
                    attr.converters.extend(a.converters)

            if reverse.is_collection: # one-to-many:
                generate_columns()
            # one-to-one:
            elif attr.is_required:
                assert not reverse.is_required
                generate_columns()
            elif reverse.is_required:
                if attr.columns: raise MappingError(
                    "Parameter 'column' cannot be specified for attribute %s. "
                    "Specify this parameter for reverse attribute %s or make %s optional"
                    % (attr, reverse, reverse))
            elif reverse.columns:
                if attr.columns: raise MappingError(
                    "Both attributes %s and %s have parameter 'column'. "
                    "Parameter 'column' cannot be specified at both sides of one-to-one relation"
                    % (attr, reverse))
            elif attr.entity.__name__ > reverse.entity.__name__: pass
            else: generate_columns()
        attr._columns_checked = True
        if len(attr.columns) == 1: attr.column = attr.columns[0]
        else: attr.column = None
        return attr.columns
    @property
    def asc(attr):
        return attr
    @property
    def desc(attr):
        return DescWrapper(attr)

class Optional(Attribute):
    __slots__ = []
    
class Required(Attribute):
    __slots__ = []

class Unique(Required):
    __slots__ = []
    def __new__(cls, *args, **keyargs):
        is_pk = issubclass(cls, PrimaryKey)
        if not args: raise TypeError('Invalid count of positional arguments')
        attrs = tuple(a for a in args if isinstance(a, Attribute))
        non_attrs = [ a for a in args if not isinstance(a, Attribute) ]
        if attrs and (non_attrs or keyargs): raise TypeError('Invalid arguments')
        cls_dict = sys._getframe(1).f_locals
        keys = cls_dict.setdefault('_keys_', {})

        if not attrs:
            result = Required.__new__(cls, *args, **keyargs)
            keys[(result,)] = is_pk
            return result

        for attr in attrs:
            if attr.is_collection or (is_pk and not attr.is_required and not attr.auto): raise TypeError(
                '%s attribute cannot be part of %s' % (attr.__class__.__name__, is_pk and 'primary key' or 'unique index'))
            attr.is_indexed = True
        if len(attrs) == 1:
            attr = attrs[0]
            if attr.is_required: raise TypeError('Invalid declaration')
            attr.is_unique = True
        else:
            for i, attr in enumerate(attrs): attr.composite_keys.append((attrs, i))
        keys[attrs] = is_pk
        return None

def populate_criteria_list(criteria_list, columns, converters, params_count=0, table_alias=None):
    assert len(columns) == len(converters)
    for column, converter in zip(columns, converters):
        criteria_list.append([EQ, [ COLUMN, table_alias, column ], [ PARAM, params_count, converter ] ])
        params_count += 1
    return params_count

class PrimaryKey(Unique):
    __slots__ = []

class Collection(Attribute):
    __slots__ = 'table', 'cached_load_sql', 'cached_add_m2m_sql', 'cached_remove_m2m_sql', 'wrapper_class'
    def __init__(attr, py_type, *args, **keyargs):
        if attr.__class__ is Collection: raise TypeError("'Collection' is abstract type")
        table = keyargs.pop('table', None)  # TODO: rename table to link_table or m2m_table
        if table is not None and not isinstance(table, basestring):
            raise TypeError("Parameter 'table' must be a string. Got: %r" % table)
        attr.table = table
        Attribute.__init__(attr, py_type, *args, **keyargs)
        if attr.default is not None: raise TypeError('default value could not be set for collection attribute')
        if attr.auto: raise TypeError("'auto' option could not be set for collection attribute")

        attr.cached_load_sql = None
        attr.cached_add_m2m_sql = None
        attr.cached_remove_m2m_sql = None
    def load(attr, obj):
        assert False, 'Abstract method'
    def __get__(attr, obj, cls=None):
        assert False, 'Abstract method'
    def __set__(attr, obj, val):
        assert False, 'Abstract method'
    def __delete__(attr, obj):
        assert False, 'Abstract method'
    def prepare(attr, obj, val, fromdb=False):
        assert False, 'Abstract method'
    def set(attr, obj, val, fromdb=False):
        assert False, 'Abstract method'

EMPTY = ()

class SetData(set):
    __slots__ = 'is_fully_loaded', 'added', 'removed'
    def __init__(setdata):
        setdata.is_fully_loaded = False
        setdata.added = setdata.removed = EMPTY

class Set(Collection):
    __slots__ = []
    def check(attr, val, obj=None, entity=None, from_db=False):
        assert val is not NOT_LOADED
        if val is None or val is DEFAULT: return set()
        if entity is not None: pass
        elif obj is not None: entity = obj.__class__
        else: entity = attr.entity
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        if isinstance(val, reverse.entity): items = set((val,))
        else:
            rentity = reverse.entity
            try: items = set(val)
            except TypeError: raise TypeError('Item of collection %s.%s must be an instance of %s. Got: %r'
                                              % (entity.__name__, attr.name, rentity.__name__, val))
            for item in items:
                if not isinstance(item, rentity):
                    raise TypeError('Item of collection %s.%s must be an instance of %s. Got: %r'
                                    % (entity.__name__, attr.name, rentity.__name__, item))
        if obj is not None: cache = obj._cache_
        else: cache = entity._get_cache_()
        for item in items:
            if item._cache_ is not cache:
                raise TransactionError('An attempt to mix objects belongs to different caches')
        return items
    def load(attr, obj):
        assert obj._status_ not in ('deleted', 'cancelled')
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is not NOT_LOADED and setdata.is_fully_loaded: return setdata
        reverse = attr.reverse
        if reverse is None: raise NotImplementedError
        if setdata is NOT_LOADED: setdata = obj._curr_[attr.name] = SetData()
        if not reverse.is_collection:
            reverse.entity._find_(None, (), {reverse.name:obj})
        else:
            database = obj._diagram_.database
            if attr.cached_load_sql is None:
                sql_ast = attr.construct_sql_m2m()
                sql, adapter = database._ast2sql(sql_ast)
                attr.cached_load_sql = sql, adapter
            else: sql, adapter = attr.cached_load_sql
            values = obj._get_raw_pkval_()
            arguments = adapter(values)
            cursor = database._exec_sql(sql, arguments)
            items = []
            for row in cursor.fetchall():
                item = attr.py_type._get_by_raw_pkval_(row)
                if item in setdata: continue
                if item in setdata.removed: continue
                items.append(item)
                setdata.add(item)
            reverse.db_reverse_add(items, obj)
        setdata.is_fully_loaded = True
        return setdata
    def construct_sql_m2m(attr):
        reverse = attr.reverse
        assert reverse is not None and reverse.is_collection and issubclass(reverse.py_type, Entity)
        table_name = attr.table
        assert table_name is not None
        select_list = [ ALL ]
        for column in attr.columns:
            select_list.append([COLUMN, 'T1', column ])
        from_list = [ FROM, [ 'T1', TABLE, table_name ]]
        criteria_list = [ AND ]
        assert len(reverse.columns) == len(reverse.converters)
        for i, (column, converter) in enumerate(zip(reverse.columns, reverse.converters)):
            criteria_list.append([EQ, [COLUMN, 'T1', column], [ PARAM, i, converter ]])
        sql_ast = [ SELECT, select_list, from_list, [ WHERE, criteria_list ] ]
        return sql_ast
    def copy(attr, obj):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED or not setdata.is_fully_loaded: setdata = attr.load(obj)
        reverse = attr.reverse
        if reverse.is_collection or reverse.pk_offset is not None: return setdata.copy()
        for item in setdata:
            bit = item._bits_[reverse]
            wbits = item._wbits_
            if wbits is not None and not wbits & bit: item._rbits_ |= bit
        return setdata.copy()
    def __get__(attr, obj, cls=None):
        if obj is None: return attr
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        rentity = attr.py_type
        wrapper_class = rentity._get_set_wrapper_subclass_()
        return wrapper_class(obj, attr)
    def __set__(attr, obj, val, undo_funcs=None):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        items = attr.check(val, obj)
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED:
            if obj._status_ == 'created':
                setdata = obj._curr_[attr.name] = SetData()
                setdata.is_fully_loaded = True
                if not items: return
            else: setdata = attr.load(obj)
        elif not setdata.is_fully_loaded: setdata = attr.load(obj)
        to_add = set(ifilterfalse(setdata.__contains__, items))
        to_remove = setdata - items
        if undo_funcs is None: undo_funcs = []
        try:
            if not reverse.is_collection:
                for item in to_remove: reverse.__set__(item, None, undo_funcs)
                for item in to_add: reverse.__set__(item, obj, undo_funcs)
            else:
                reverse.reverse_remove(to_remove, obj, undo_funcs)
                reverse.reverse_add(to_add, obj, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        setdata.clear()
        setdata.update(items)
        if to_add:
            if setdata.added is EMPTY: setdata.added = to_add
            else: setdata.added.update(to_add)
            if setdata.removed is not EMPTY: setdata.removed -= to_add
        if to_remove:
            if setdata.removed is EMPTY: setdata.removed = to_remove
            else: setdata.removed.update(to_remove)
            if setdata.added is not EMPTY: setdata.added -= to_remove
        cache = obj._cache_
        cache.modified_collections.setdefault(attr, set()).add(obj)
    def __delete__(attr, obj):
        raise NotImplementedError
    def reverse_add(attr, objects, item, undo_funcs):
        undo = []
        cache = item._cache_
        objects_with_modified_collections = cache.modified_collections.setdefault(attr, set())
        for obj in objects:
            setdata = obj._curr_.get(attr.name, NOT_LOADED)
            if setdata is NOT_LOADED:
                setdata = obj._curr_[attr.name] = SetData()
            if setdata.added is EMPTY: setdata.added = set()  
            elif item in setdata.added: raise AssertionError
            in_setdata = item in setdata
            in_removed = item in setdata.removed
            was_modified_earlier = obj in objects_with_modified_collections
            undo.append((obj, in_setdata, in_removed, was_modified_earlier))
            if not in_setdata: setdata.add(item)
            setdata.added.add(item)
            if in_removed: setdata.removed.remove(item)
            objects_with_modified_collections.add(obj)
        def undo_func():
            for obj, in_setdata, in_removed, was_modified_earlier in undo:
                setdata = obj._curr_[attr.name]
                setdata.added.remove(item)
                if not in_setdata: setdata.remove(item)
                if in_removed: setdata.removed.add(item)
                if not was_modified_earlier: objects_with_modified_collections.remove(obj)
        undo_funcs.append(undo_func)
    def db_reverse_add(attr, objects, item):
        for obj in objects:
            setdata = obj._curr_.get(attr.name, NOT_LOADED)
            if setdata is NOT_LOADED:
                setdata = obj._curr_[attr.name] = SetData()
            elif setdata.is_fully_loaded:
                raise UnrepeatableReadError('Phantom object %r appeared in collection %r.%s' % (item, obj, attr.name))
            setdata.add(item)
    def reverse_remove(attr, objects, item, undo_funcs):
        undo = []
        cache = item._cache_
        objects_with_modified_collections = cache.modified_collections.setdefault(attr, set())
        for obj in objects:
            setdata = obj._curr_.get(attr.name, NOT_LOADED)
            if setdata is NOT_LOADED:
                setdata = obj._curr_[attr.name] = SetData()
            if setdata.removed is EMPTY: setdata.removed = set()
            elif item in setdata.removed: raise AssertionError
            in_setdata = item in setdata
            in_added = item in setdata.added
            was_modified_earlier = obj in objects_with_modified_collections
            undo.append((obj, in_setdata, in_added, was_modified_earlier))
            if in_setdata: setdata.remove(item)
            if in_added: setdata.added.remove(item)
            setdata.removed.add(item)
            objects_with_modified_collections.add(obj)
        def undo_func():
            for obj, in_setdata, in_removed, was_modified_earlier in undo:
                setdata = obj._curr_[attr.name]
                if in_added: setdata.added.add(item)
                if in_setdata: setdata.add(item)
                setdata.removed.remove(item)
                if not was_modified_earlier: objects_with_modified_collections.remove(obj)
        undo_funcs.append(undo_func)
    def db_reverse_remove(attr, objects, item):
        raise AssertionError
    def get_m2m_columns(attr):
        if attr._columns_checked: return attr.reverse.columns
        entity = attr.entity
        reverse = attr.reverse
        if reverse.columns:
            if len(reverse.columns) != len(entity._get_pk_columns_()): raise MappingError(
                'Invalid number of columns for %s' % reverse)
        else:
            columns = entity._get_pk_columns_()
            if len(columns) == 1: reverse.columns = [ entity.__name__.lower() ]
            else:
                prefix = entity.__name__.lower() + '_'
                reverse.columns = [ prefix + column for column in columns ]
        reverse.converters = entity._pk_converters_
        attr._columns_checked = True
        return reverse.columns
    def remove_m2m(attr, removed):
        entity = attr.entity
        database = entity._diagram_.database
        cached_sql = attr.cached_remove_m2m_sql
        if cached_sql is None:
            reverse = attr.reverse
            table = attr.table
            assert table is not None
            criteria_list = [ AND ]
            for i, (column, converter) in enumerate(zip(reverse.columns + attr.columns, reverse.converters + attr.converters)):
                criteria_list.append([ EQ, [COLUMN, None, column], [ PARAM, i, converter ] ])
            sql_ast = [ DELETE, table, [ WHERE, criteria_list ] ]
            sql, adapter = database._ast2sql(sql_ast)
            attr.cached_remove_m2m_sql = sql, adapter
        else: sql, adapter = cached_sql
        arguments_list = [ adapter(obj._get_raw_pkval_() + robj._get_raw_pkval_())
                           for obj, robj in removed ]
        database._exec_sql_many(sql, arguments_list)
    def add_m2m(attr, added):
        entity = attr.entity
        database = entity._diagram_.database
        cached_sql = attr.cached_add_m2m_sql
        if cached_sql is None:
            reverse = attr.reverse
            table = attr.table
            assert table is not None
            columns = []
            params = []
            for i, (column, converter) in enumerate(zip(reverse.columns + attr.columns, reverse.converters + attr.converters)):
                columns.append(column)
                params.append([PARAM, i, converter])
            sql_ast = [ INSERT, table, columns, params ]
            sql, adapter = database._ast2sql(sql_ast)
            attr.cached_add_m2m_sql = sql, adapter
        else: sql, adapter = cached_sql
        arguments_list = [ adapter(obj._get_raw_pkval_() + robj._get_raw_pkval_())
                           for obj, robj in added ]
        database._exec_sql_many(sql, arguments_list)

class SetWrapper(object):
    __slots__ = '_obj_', '_attr_'
    def __init__(wrapper, obj, attr):
        wrapper._obj_ = obj
        wrapper._attr_ = attr
    def copy(wrapper):
        return wrapper._attr_.copy(wrapper._obj_)
    def __repr__(wrapper):
        return '%r.%s => %r' % (wrapper._obj_, wrapper._attr_.name, wrapper.copy())
    def __str__(wrapper):
        return str(wrapper.copy())
    def __nonzero__(wrapper):
        attr = wrapper._attr_
        obj = wrapper._obj_
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED: setdata = attr.load(obj)
        if setdata: return True
        if not setdata.is_fully_loaded: setdata = attr.load(obj)
        return bool(setdata)
    def __len__(wrapper):
        attr = wrapper._attr_
        obj = wrapper._obj_
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED or not setdata.is_fully_loaded: setdata = attr.load(obj)
        return len(setdata)
    def __iter__(wrapper):
        return iter(wrapper.copy())
    def __eq__(wrapper, x):
        if isinstance(x, SetWrapper):
            if wrapper._obj_ is x._obj_ and wrapper._attr_ is x._attr_: return True
            else: x = x.copy()
        elif not isinstance(x, set): x = set(x)
        items = wrapper.copy()
        return items == x
    def __ne__(wrapper, x):
        return not wrapper.__eq__(x)
    def __add__(wrapper, x):
        return wrapper.copy().union(x)
    def __sub__(wrapper, x):
        return wrapper.copy().difference(x)
    def __contains__(wrapper, item):
        obj = wrapper._obj_
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        attr = wrapper._attr_
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is not NOT_LOADED:
            if item in setdata: return True
            if setdata.is_fully_loaded: return False
        setdata = attr.load(obj)
        return item in setdata
    def add(wrapper, x):
        obj = wrapper._obj_
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        attr = wrapper._attr_
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        items = attr.check(x, obj)
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED: setdata = obj._curr_[attr.name] = SetData()
        items.difference_update(setdata.added)
        undo_funcs = []
        try:
            if not reverse.is_collection:
                  for item in items - setdata: reverse.__set__(item, obj, undo_funcs)
            else: reverse.reverse_add(items - setdata, obj, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        setdata.update(items)
        if setdata.added is EMPTY: setdata.added = items
        else: setdata.added.update(items)
        if setdata.removed is not EMPTY: setdata.removed -= items
        obj._cache_.modified_collections.setdefault(attr, set()).add(obj)
    def __iadd__(wrapper, x):
        wrapper.add(x)
        return wrapper
    def remove(wrapper, x):
        obj = wrapper._obj_
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        attr = wrapper._attr_
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        items = attr.check(x, obj)
        setdata = obj._curr_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED or not setdata.is_fully_loaded:
            setdata = attr.load(obj) # TODO: Load only the necessary objects
        items.difference_update(setdata.removed)
        undo_funcs = []
        try:
            if not reverse.is_collection:
                for item in (items & setdata): reverse.__set__(item, None, undo_funcs)
            else: reverse.reverse_remove(items & setdata, obj, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        setdata -= items
        if setdata.added is not EMPTY: setdata.added -= items
        if setdata.removed is EMPTY: setdata.removed = items
        else: setdata.removed.update(items)
        obj._cache_.modified_collections.setdefault(attr, set()).add(obj)
    def __isub__(wrapper, x):
        wrapper.remove(x)
        return wrapper
    def clear(wrapper):
        obj = wrapper._obj_
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        wrapper._attr_.__set__(obj, None)

class PropagatedSet(object):
    __slots__ = [ '_items_' ]
    def __init__(pset, items):
        pset._items_ = frozenset(items)
    def __repr__(pset):
        s = ', '.join(map(repr, sorted(pset._items_)))
        return '%s([%s])' % (pset.__class__.__name__, s)
    def __nonzero__(pset):
        return bool(pset._items_)
    def __len__(pset):
        return len(pset._items_)
    def __iter__(pset):
        return iter(pset._items_)
    def __eq__(pset, x):
        if isinstance(x, PropagatedSet):
            return pset._items_ == x._items_
        if isinstance(x, (set, frozenset)):
            return pset._items_ == x
        return pset._items_ == frozenset(x)
    def __ne__(pset, x):
        return not pset.__eq__(x)
    def __contains__(pset, item):
        return item in pset._items_

##class List(Collection): pass
##class Dict(Collection): pass
##class Relation(Collection): pass

class EntityIter(object):
    def __init__(self, entity):
        self.entity = entity
    def next(self):
        raise StopIteration
    
next_entity_id = count(1).next
next_new_instance_id = count(1).next

class EntityMeta(type):
    def __setattr__(entity, name, val):
        if name.startswith('_') and name.endswith('_'):
            type.__setattr__(entity, name, val)
        else: raise NotImplementedError
    def __new__(meta, name, bases, cls_dict):
        if 'Entity' in globals():
            if '__slots__' in cls_dict: raise TypeError('Entity classes cannot contain __slots__ variable')
            cls_dict['__slots__'] = ()
        return super(EntityMeta, meta).__new__(meta, name, bases, cls_dict)
    def __init__(entity, name, bases, cls_dict):
        super(EntityMeta, entity).__init__(name, bases, cls_dict)
        if 'Entity' not in globals(): return
        outer_dict = sys._getframe(1).f_locals

        diagram = cls_dict.pop('_diagram_', None) or outer_dict.get('_diagram_')
        if diagram is None:
            diagram = Diagram()
            outer_dict['_diagram_'] = diagram

        entity._cls_init_(diagram)
    def __iter__(entity):
        return EntityIter(entity)
    def all(entity, *args, **keyargs):
        return entity._find_(None, args, keyargs)
    def get(entity, *args, **keyargs):
        objects = entity._find_(1, args, keyargs)
        if not objects: return None
        if len(objects) > 1: raise MultipleObjectsFoundError(
            'Multiple objects was found. Use %s.all(...) to retrieve them' % entity.__name__)
        return objects[0]
    def __getitem__(entity, key):
        if type(key) is tuple: args = key
        else: args = (key,)
        objects = entity._find_(1, args, {})
        if not objects: raise ObjectNotFound(entity, key)
        if len(objects) > 1: raise MultipleObjectsFoundError(
            'Multiple objects was found. Use %s.all(...) to retrieve them' % entity.__name__)
        return objects[0]
    def where(entity, func):
        if not isinstance(func, types.FunctionType): raise TypeError
        globals = sys._getframe(1).f_globals
        locals = sys._getframe(1).f_locals
        return entity._query_from_lambda_(func, globals, locals)
    def orderby(entity, *args):
        name = (''.join(letter for letter in entity.__name__ if letter.isupper())).lower() or entity.__name__[0]
        for_expr = ast.GenExprFor(ast.AssName(name, 'OP_ASSIGN'), ast.Name('.0'), [])
        inner_expr = ast.GenExprInner(ast.Name(name), [ for_expr ])
        query = Query(None, inner_expr, set(['.0']), {}, { '.0' : entity })
        return query.orderby(*args)
    def _find_(entity, max_objects_count, args, keyargs):
        if args and isinstance(args[0], types.FunctionType):
            if len(args) > 1: raise TypeError
            if keyargs: raise TypeError
            func = args[0]
            globals = sys._getframe(2).f_globals
            locals = sys._getframe(2).f_locals
            query = entity._query_from_lambda_(func, globals, locals)
            return query.all()

        pkval, avdict = entity._normalize_args_(args, keyargs, False)
        for attr in avdict:
            if attr.is_collection: raise TypeError(
                'Collection attribute %s.%s cannot be specified as search criteria' % (attr.entity.__name__, attr.name))
        try:
            objects = entity._find_in_cache_(pkval, avdict)
        except KeyError:
            objects = entity._find_in_db_(avdict, max_objects_count)
        return objects
    def _query_from_lambda_(entity, func, globals, locals):
        names, argsname, keyargsname, defaults = inspect.getargspec(func)
        if len(names) > 1: raise TypeError
        if argsname or keyargsname: raise TypeError
        if defaults: raise TypeError
        name = names[0]

        cond_expr, external_names = decompile(func)
        external_names.discard(name)
        external_names.add('.0')

        if_expr = ast.GenExprIf(cond_expr)
        for_expr = ast.GenExprFor(ast.AssName(name, 'OP_ASSIGN'), ast.Name('.0'), [ if_expr ])
        inner_expr = ast.GenExprInner(ast.Name(name), [ for_expr ])

        locals = locals.copy()
        assert '.0' not in locals
        locals['.0'] = entity

        return Query(func.func_code, inner_expr, external_names, globals, locals)

class Entity(object):
    __metaclass__ = EntityMeta
    __slots__ = '_cache_', '_status_', '_pkval_', '_newid_', '_prev_', '_curr_', '_rbits_', '_wbits_', '__weakref__'
    @classmethod
    def _cls_init_(entity, diagram):
        if entity.__name__ in diagram.entities:
            raise DiagramError('Entity %s already exists' % entity.__name__)
        entity._id_ = next_entity_id()
        direct_bases = [ c for c in entity.__bases__ if issubclass(c, Entity) and c is not Entity ]
        entity._direct_bases_ = direct_bases
        entity._all_bases_ = set((entity,))
        for base in direct_bases: entity._all_bases_.update(base._all_bases_)
        if direct_bases:
            roots = set(base._root_ for base in direct_bases)
            if len(roots) > 1: raise DiagramError(
                'With multiple inheritance of entities, inheritance graph must be diamond-like')
            entity._root_ = roots.pop()
            for base in direct_bases:
                if base._diagram_ is not diagram: raise DiagramError(
                    'When use inheritance, base and derived entities must belong to same diagram')
        else: entity._root_ = entity

        base_attrs = []
        base_attrs_dict = {}
        for base in direct_bases:
            for a in base._attrs_:
                if base_attrs_dict.setdefault(a.name, a) is not a: raise DiagramError('Ambiguous attribute name %s' % a.name)
                base_attrs.append(a)
        entity._base_attrs_ = base_attrs

        new_attrs = []
        for name, attr in entity.__dict__.items():
            if name in base_attrs_dict: raise DiagramError("Name '%s' hides base attribute %s" % (name,base_attrs_dict[name]))
            if not isinstance(attr, Attribute): continue
            if name.startswith('_') and name.endswith('_'): raise DiagramError(
                'Attribute name cannot both starts and ends with underscore. Got: %s' % name)
            if attr.entity is not None: raise DiagramError(
                'Duplicate use of attribute %s in entity %s' % (attr, entity.__name__))
            attr._init_(entity, name)
            new_attrs.append(attr)
        new_attrs.sort(key=attrgetter('id'))

        keys = entity.__dict__.get('_keys_', {})
        for key in keys:
            for attr in key:
                assert isinstance(attr, Attribute) and not attr.is_collection
                if attr.entity is not entity: raise DiagramError(
                    'Invalid use of attribute %s in entity %s' % (attr, entity.__name__))
        primary_keys = set(key for key, is_pk in keys.items() if is_pk)
        if direct_bases:
            if primary_keys: raise DiagramError('Primary key cannot be redefined in derived classes')
            for base in direct_bases:
                keys[base._pk_attrs_] = True
                for key in base._keys_: keys[key] = False
            primary_keys = set(key for key, is_pk in keys.items() if is_pk)

        if len(primary_keys) > 1: raise DiagramError('Only one primary key can be defined in each entity class')
        elif not primary_keys:
            if hasattr(entity, 'id'): raise DiagramError(
                "Cannot create primary key for %s automatically because name 'id' is alredy in use" % entity.__name__)
            _keys_ = {}
            attr = PrimaryKey(int, auto=True) # Side effect: modifies _keys_ local variable
            attr._init_(entity, 'id')
            type.__setattr__(entity, 'id', attr)  # entity.id = attr
            new_attrs.insert(0, attr)
            key, is_pk = _keys_.popitem()
            keys[key] = True
            pk_attrs = key
        else: pk_attrs = primary_keys.pop()
        for i, attr in enumerate(pk_attrs): attr.pk_offset = i
        entity._pk_columns_ = None
        entity._pk_attrs_ = pk_attrs
        entity._pk_is_composite_ = len(pk_attrs) > 1
        entity._pk_ = len(pk_attrs) > 1 and pk_attrs or pk_attrs[0]
        entity._keys_ = [ key for key, is_pk in keys.items() if not is_pk ]
        entity._simple_keys_ = [ key[0] for key in entity._keys_ if len(key) == 1 ]
        entity._composite_keys_ = [ key for key in entity._keys_ if len(key) > 1 ]

        entity._new_attrs_ = new_attrs
        entity._attrs_ = base_attrs + new_attrs
        entity._adict_ = dict((attr.name, attr) for attr in entity._attrs_)

        entity._bits_ = {}
        next_offset = count().next
        all_bits = 0
        for attr in entity._attrs_:
            if attr.is_collection or attr.pk_offset is not None: continue
            next_bit = 1 << next_offset()
            entity._bits_[attr] = next_bit
            all_bits |= next_bit
        entity._all_bits_ = all_bits

        try: table_name = entity.__dict__['_table_']
        except KeyError: entity._table_ = None
        else:
            if not isinstance(table_name, basestring): raise TypeError(
                '%s._table_ property must be a string. Got: %r' % (entity.__name__, table_name))

        entity._diagram_ = diagram
        diagram.entities[entity.__name__] = entity
        entity._link_reverse_attrs_()

        entity._cached_create_sql_ = None
        entity._cached_delete_sql_ = None
        entity._find_sql_cache_ = {}
        entity._update_sql_cache_ = {}
        entity._lock_sql_cache_ = {}

        entity._propagation_mixin_ = None
        entity._set_wrapper_subclass_ = None
        entity._propagated_set_subclass_ = None
    @classmethod
    def _link_reverse_attrs_(entity):
        diagram = entity._diagram_
        unmapped_attrs = diagram.unmapped_attrs.pop(entity.__name__, set())
        for attr in entity._new_attrs_:
            py_type = attr.py_type
            if isinstance(py_type, basestring):
                entity2 = diagram.entities.get(py_type)
                if entity2 is None:
                    diagram.unmapped_attrs.setdefault(py_type, set()).add(attr)
                    continue
                attr.py_type = py_type = entity2
            elif not issubclass(py_type, Entity): continue
            
            entity2 = py_type
            if entity2._diagram_ is not diagram:
                raise DiagramError('Interrelated entities must belong to same diagram. '
                                   'Entities %s and %s belongs to different diagrams'
                                   % (entity.__name__, entity2.__name__))
            reverse = attr.reverse
            if isinstance(reverse, basestring):
                attr2 = getattr(entity2, reverse, None)
                if attr2 is None: raise DiagramError('Reverse attribute %s.%s not found' % (entity2.__name__, reverse))
            elif isinstance(reverse, Attribute):
                attr2 = reverse
                if attr2.entity is not entity2: raise DiagramError('Incorrect reverse attribute %s used in %s' % (attr2, attr)) ###
            elif reverse is not None: raise DiagramError("Value of 'reverse' option must be string. Got: %r" % type(reverse))
            else:
                candidates1 = []
                candidates2 = []
                for attr2 in entity2._new_attrs_:
                    if attr2.py_type not in (entity, entity.__name__): continue
                    reverse2 = attr2.reverse
                    if reverse2 in (attr, attr.name): candidates1.append(attr2)
                    elif not reverse2: candidates2.append(attr2)
                msg = 'Ambiguous reverse attribute for %s'
                if len(candidates1) > 1: raise DiagramError(msg % attr)
                elif len(candidates1) == 1: attr2 = candidates1[0]
                elif len(candidates2) > 1: raise DiagramError(msg % attr)
                elif len(candidates2) == 1: attr2 = candidates2[0]
                else: raise DiagramError('Reverse attribute for %s not found' % attr)

            type2 = attr2.py_type
            msg = 'Inconsistent reverse attributes %s and %s'
            if isinstance(type2, basestring):
                if type2 != entity.__name__: raise DiagramError(msg % (attr, attr2))
                attr2.py_type = entity
            elif type2 != entity: raise DiagramError(msg % (attr, attr2))
            reverse2 = attr2.reverse
            if reverse2 not in (None, attr, attr.name): raise DiagramError(msg % (attr,attr2))

            if attr.is_required and attr2.is_required: raise DiagramError(
                "At least one attribute of one-to-one relationship %s - %s must be optional" % (attr, attr2))

            attr.reverse = attr2
            attr2.reverse = attr
            unmapped_attrs.discard(attr2)          
        for attr in unmapped_attrs:
            raise DiagramError('Reverse attribute for %s.%s was not found' % (attr.entity.__name__, attr.name))
    @classmethod
    def _get_cache_(entity):
        database = entity._diagram_.database
        if database is None: raise TransactionError
        return database._get_cache()
    def __new__(entity, *args, **keyargs):
        raise TypeError('Use %(name)s.create(...) or %(name)s.get(...) instead of %(name)s(...)' % dict(name=entity.__name__))
    @classmethod
    def _get_pk_columns_(entity):
        if entity._pk_columns_ is not None: return entity._pk_columns_
        pk_columns = []
        pk_converters = []
        pk_paths = []
        for attr in entity._pk_attrs_:
            attr_columns = attr.get_columns()
            attr_col_paths = attr.col_paths
            pk_columns.extend(attr_columns)
            pk_converters.extend(attr.converters)
            pk_paths.extend(attr_col_paths)
        entity._pk_columns_ = pk_columns
        entity._pk_converters_ = pk_converters
        entity._pk_nones_ = (None,) * len(pk_columns)
        entity._pk_paths_ = pk_paths
        return pk_columns
    def _get_raw_pkval_(obj):
        pkval = obj._pkval_
        if not obj._pk_is_composite_:
            if not obj.__class__._pk_.reverse: return (pkval,)
            else: return pkval._get_raw_pkval_()
        raw_pkval = []
        append = raw_pkval.append
        for attr, val in zip(obj._pk_attrs_, pkval):
            if not attr.reverse: append(val)
            else: raw_pkval += val._get_raw_pkval_()
        return tuple(raw_pkval)
    def __repr__(obj):
        pkval = obj._pkval_
        if pkval is None: return '%s(new:%d)' % (obj.__class__.__name__, obj._newid_)
        elif obj._pk_is_composite_: return '%s%r' % (obj.__class__.__name__, pkval)
        else: return '%s(%r)' % (obj.__class__.__name__, pkval)
    @classmethod
    def _new_(entity, pkval, status, raw_pkval=None, undo_funcs=None):
        cache = entity._get_cache_()
        index = cache.indexes.setdefault(entity._pk_, {})
        if pkval is None: obj = None
        else: obj = index.get(pkval)
        if obj is None: pass
        elif status == 'created':
            if entity._pk_is_composite_: pkval = ', '.join(str(item) for item in pkval)
            raise IndexError('Cannot create %s: instance with primary key %s already exists'
                             % (obj.__class__.__name__, pkval))                
        else: return obj
        obj = object.__new__(entity)
        obj._prev_ = {}
        obj._curr_ = {}
        obj._cache_ = cache
        obj._status_ = status
        obj._pkval_ = pkval
        if pkval is not None:
            index[pkval] = obj
            obj._newid_ = None
        else: obj._newid_ = next_new_instance_id()
        if obj._pk_is_composite_: pairs = zip(entity._pk_attrs_, pkval)
        else: pairs = ((entity._pk_, pkval),)
        if status == 'loaded':
            assert undo_funcs is None
            obj._rbits_ = obj._wbits_ = 0
            for attr, val in pairs:
                obj._curr_[attr.name] = val
                if attr.reverse: attr.db_update_reverse(obj, NOT_LOADED, val)
        elif status == 'created':
            assert undo_funcs is not None
            obj._rbits_ = obj._wbits_ = None
            for attr, val in pairs:
                obj._curr_[attr.name] = val
                if attr.reverse: attr.update_reverse(obj, NOT_LOADED, val, undo_funcs)
        else: assert False
        return obj
    @classmethod
    def _get_by_raw_pkval_(entity, raw_pkval):
        i = 0
        pkval = []
        for attr in entity._pk_attrs_:
            if attr.column is not None:
                val = raw_pkval[i]
                i += 1
                if not attr.reverse: val = attr.check(val, None, entity, from_db=True)
                else: val = attr.py_type._get_by_raw_pkval_((val,))
            else:
                if not attr.reverse: raise NotImplementedError
                vals = raw_pkval[i:i+len(attr.columns)]
                val = attr.py_type._get_by_raw_pkval_(vals)
            pkval.append(val)
        if not entity._pk_is_composite_: pkval = pkval[0]
        else: pkval = tuple(pkval)
        obj = entity._new_(pkval, 'loaded', raw_pkval)
        assert obj._status_ not in ('deleted', 'cancelled')
        return obj
    @classmethod
    def _find_in_cache_(entity, pkval, avdict):
        cache = entity._get_cache_()
        obj = None
        if pkval is not None:
            index = cache.indexes.get(entity._pk_)
            if index is not None: obj = index.get(pkval)
        if obj is None:
            for attr in ifilter(avdict.__contains__, entity._simple_keys_):
                index = cache.indexes.get(attr)
                if index is None: continue
                val = avdict[attr]
                obj = index.get(val)
                if obj is not None: break
        if obj is None:
            NOT_FOUND = object()
            for attrs in entity._composite_keys_:
                vals = tuple(avdict.get(attr, NOT_FOUND) for attr in attrs)
                if NOT_FOUND in vals: continue
                index = cache.indexes.get(attrs)
                if index is None: continue
                obj = index.get(vals)
                if obj is not None: break
        if obj is None:
            for attr, val in avdict.iteritems():
                if val is None: continue
                reverse = attr.reverse
                if reverse and not reverse.is_collection:
                    obj = reverse.__get__(val)
                    break
        if obj is None:
            for attr, val in avdict.iteritems():
                if isinstance(val, Entity) and val._pkval_ is None:
                    reverse = attr.reverse
                    if not reverse.is_collection:
                        obj = reverse.__get__(val)
                        if obj is None: return []
                    elif isinstance(reverse, Set):
                        filtered_objects = []
                        for obj in reverse.__get__(val):
                            for attr, val in avdict.iteritems():
                                if val != attr.get(obj): break
                            else: filtered_objects.append(obj)
                        filtered_objects.sort(key=entity._get_raw_pkval_)
                        return filtered_objects
                    else: raise NotImplementedError
        if obj is not None:
            for attr, val in avdict.iteritems():
                if val != attr.__get__(obj): return []
            return [ obj ]
        raise KeyError
    def _load_(obj):
        if obj._pk_is_composite_:
            avdict = dict((attr, val) for attr, val in zip(obj._pk_attrs_, obj._pkval_))
        else: avdict = { obj.__class__._pk_ : obj._pkval_ }
        objects = obj._find_in_db_(avdict, 1)        
        if not objects: raise UnrepeatableReadError('%s disappeared' % obj)
        assert len(objects) == 1 and obj == objects[0]
    @classmethod
    def _construct_select_clause_(entity, alias=None, distinct=False):
        table_name = entity._table_
        attr_offsets = {}
        if distinct: select_list = [ DISTINCT ]
        else: select_list = [ ALL ]
        for attr in entity._attrs_:
            if attr.is_collection: continue
            if not attr.columns: continue
            attr_offsets[attr] = len(select_list) - 1
            for column in attr.columns:
                select_list.append([ COLUMN, alias, column ])
        return select_list, attr_offsets
    @classmethod
    def _construct_sql_(entity, query_attrs, max_rows_count=None):
        table_name = entity._table_
        select_list, attr_offsets = entity._construct_select_clause_()
        from_list = [ FROM, [ None, TABLE, table_name ]]

        criteria_list = [ AND ]
        values = []
        extractors = {}
        for attr, attr_is_none in query_attrs:
            if not attr.reverse:
                if not attr_is_none:
                    assert len(attr.converters) == 1
                    criteria_list.append([EQ, [COLUMN, None, attr.column], [ PARAM, attr.name, attr.converters[0] ]])
                    extractors[attr.name] = lambda avdict, attr=attr: avdict[attr]
                else: criteria_list.append([IS_NULL, [COLUMN, None, attr.column]])
            elif not attr.columns: raise NotImplementedError
            else:
                attr_entity = attr.py_type
                assert attr_entity == attr.reverse.entity
                if len(attr_entity._pk_columns_) == 1:
                    if not attr_is_none:
                        assert len(attr.converters) == 1
                        criteria_list.append([EQ, [COLUMN, None, attr.column], [ PARAM, attr.name, attr.converters[0] ]])
                        extractors[attr.name] = lambda avdict, attr=attr: avdict[attr]._get_raw_pkval_()[0]
                    else: criteria_list.append([IS_NULL, [COLUMN, None, attr.column]])
                elif not attr_is_none:
                    for i, (column, converter) in enumerate(zip(attr_entity._pk_columns_, attr_entity._pk_converters_)):
                        param_name = '%s-%d' % (attr.name, i+1)
                        criteria_list.append([EQ, [COLUMN, None, column], [ PARAM, param_name, converter ]])
                        extractors[param_name] = lambda avdict, attr=attr, i=i: avdict[attr]._get_raw_pkval_()[i]
                else:
                    for column in attr_entity._pk_columns_:
                        criteria_list.append([IS_NULL, [COLUMN, None, column]])

        sql_ast = [ SELECT, select_list, from_list ]
        if len(criteria_list) > 1: sql_ast.append([ WHERE, criteria_list  ])
        if max_rows_count <> 1:
            sql_ast.append([ ORDER_BY ] + [ ([COLUMN, None, column], ASC) for column in entity._pk_columns_ ])
        if max_rows_count is not None:
            sql_ast.append([ LIMIT, [ VALUE, max_rows_count + 1 ] ])
        def extractor(avdict):
            param_dict = {}
            for param, extractor in extractors.iteritems():
                param_dict[param] = extractor(avdict)
            return param_dict
        return sql_ast, extractor, attr_offsets
    @classmethod
    def _find_in_db_(entity, avdict, max_rows_count=None):
        if max_rows_count is None: max_rows_count = options.MAX_ROWS_COUNT
        database = entity._diagram_.database
        query_attrs = tuple((attr, value is None) for attr, value in sorted(avdict.iteritems()))
        query_key = query_attrs, max_rows_count
        cached_sql = entity._find_sql_cache_.get(query_key)
        if cached_sql is None:
            sql_ast, extractor, attr_offsets = entity._construct_sql_(query_attrs, max_rows_count)
            sql, adapter = database._ast2sql(sql_ast)
            cached_sql = sql, extractor, adapter, attr_offsets
            entity._find_sql_cache_[query_key] = cached_sql
        else: sql, extractor, adapter, attr_offsets = cached_sql
        value_dict = extractor(avdict)
        arguments = adapter(value_dict)
        cursor = database._exec_sql(sql, arguments)
        objects = entity._fetch_objects(cursor, attr_offsets, max_rows_count)
        return objects
    @classmethod
    def _fetch_objects(entity, cursor, attr_offsets, max_rows_count=None):
        if max_rows_count is not None:
            rows = cursor.fetchmany(max_rows_count + 1)
            if len(rows) == max_rows_count + 1:
                if max_rows_count == 1: raise MultipleObjectsFoundError(
                    'Multiple objects was found. Use %s.all(...) to retrieve them' % entity.__name__)
                raise TooManyObjectsFoundError(
                    'Found more then pony.options.MAX_ROWS_COUNT=%d objects' % options.MAX_ROWS_COUNT)
        else: rows = cursor.fetchall()
        objects = []
        for row in rows:
            pkval, avdict = entity._parse_row_(row, attr_offsets)
            obj = entity._new_(pkval, 'loaded')
            if obj._status_ in ('deleted', 'cancelled'): continue
            obj._db_set_(avdict)
            objects.append(obj)
        return objects
    @classmethod
    def _parse_row_(entity, row, attr_offsets):
        avdict = {}
        for attr, i in attr_offsets.iteritems():
            if attr.column is not None:
                val = row[i]
                if not attr.reverse:  val = attr.check(val, None, entity, from_db=True)
                else: val = attr.py_type._get_by_raw_pkval_((val,))
            else:
                if not attr.reverse: raise NotImplementedError
                vals = row[i:i+len(attr.columns)]
                val = attr.py_type._get_by_raw_pkval_(vals)
            avdict[attr] = val
        if not entity._pk_is_composite_: pkval = avdict.pop(entity._pk_, None)            
        else: pkval = tuple(avdict.pop(attr, None) for attr in entity._pk_attrs_)
        return pkval, avdict
    @classmethod
    def create(entity, *args, **keyargs):
        pkval, avdict = entity._normalize_args_(args, keyargs, True)
        undo_funcs = []
        cache = entity._get_cache_()
        indexes = {}
        for attr in entity._simple_keys_:
            val = avdict[attr]
            if val in cache.indexes.setdefault(attr, {}): raise IndexError(
                'Cannot create %s: value %s for key %s already exists' % (entity.__name__, val, attr.name))
            indexes[attr] = val
        for attrs in entity._composite_keys_:
            vals = tuple(map(avdict.__getitem__, attrs))
            if vals in cache.indexes.setdefault(attrs, {}):
                attr_names = ', '.join(attr.name for attr in attrs)
                raise IndexError('Cannot create %s: value %s for composite key (%s) already exists'
                                 % (obj.__class__.__name__, vals, attr_names))
            indexes[attrs] = vals
        try:
            obj = entity._new_(pkval, 'created', None, undo_funcs)
            for attr, val in avdict.iteritems():
                if attr.pk_offset is not None: continue
                elif not attr.is_collection:
                    obj._curr_[attr.name] = val
                    if attr.reverse: attr.update_reverse(obj, None, val, undo_funcs)
                else: attr.__set__(obj, val, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        if pkval is not None:
            cache.indexes[entity._pk_][pkval] = obj
        for key, vals in indexes.iteritems():
            cache.indexes[key][vals] = obj
        cache.created.add(obj)
        cache.to_be_checked.append(obj)
        return obj
    def _db_set_(obj, avdict):
        assert obj._status_ not in ('created', 'deleted', 'cancelled')
        get_curr = obj._curr_.get
        get_prev = obj._prev_.get
        set_prev = obj._prev_.__setitem__
        rbits = obj._rbits_
        wbits = obj._wbits_
        for attr, prev in avdict.items():
            assert attr.pk_offset is None
            old_prev = get_prev(attr.name, NOT_LOADED)
            if old_prev == prev:
                del avdict[attr]
                continue
            bit = obj._bits_[attr]
            if rbits & bit: raise UnrepeatableReadError(
                'Value of %s.%s for %s was updated outside of current transaction (was: %s, now: %s)'
                % (obj.__class__.__name__, attr.name, obj, old_prev, prev))
            set_prev(attr.name, prev)
            if wbits & bit:
                del avdict[attr]
                continue
            curr = get_curr(attr.name, NOT_LOADED)
            assert curr == old_prev
        if not avdict: return
        NOT_FOUND = object()
        cache = obj._cache_
        for attr in obj._simple_keys_:
            val = avdict.get(attr, NOT_FOUND)
            if val is NOT_FOUND: continue
            curr = get_curr(attr.name, NOT_LOADED)
            if curr == val: continue
            cache.db_update_simple_index(obj, attr, curr, val)
        for attrs in obj._composite_keys_:
            for attr in attrs:
                if attr in avdict: break
            else: continue
            vals = [ get_curr(a.name, NOT_LOADED) for a in attrs ]
            currents = tuple(vals)
            for i, attr in enumerate(attrs):
                val = avdict.get(attr, NOT_FOUND)
                if val is NOT_FOUND: continue
                vals[i] = val
            vals = tuple(vals)
            cache.db_update_composite_index(obj, attrs, currents, vals)
        set_curr = obj._curr_.__setitem__
        for attr, val in avdict.iteritems():
            if attr.reverse:
                curr = get_curr(attr.name, NOT_LOADED)
                attr.db_update_reverse(obj, curr, val)
            set_curr(attr.name, val)
    def _delete_(obj, undo_funcs=None):
        is_recursive_call = undo_funcs is not None
        if not is_recursive_call: undo_funcs = []
        cache = obj._cache_
        status = obj._status_
        assert status not in ('deleted', 'cancelled')
        get_curr = obj._curr_.get
        undo_list = []
        undo_dict = {}
        def undo_func():
            obj._status_ = status
            if status in ('loaded', 'saved'):
                to_be_checked = cache.to_be_checked
                if to_be_checked and to_be_checked[-1] is obj: to_be_checked.pop()
                assert obj not in to_be_checked
            obj._curr_.update((attr.name, val) for attr, val in undo_dict.iteritems())
            for index, old_key in undo_list: index[old_key] = obj
        undo_funcs.append(undo_func)
        try:
            for attr in obj._attrs_:
                reverse = attr.reverse
                if not reverse: continue
                if not attr.is_collection:
                    val = get_curr(attr.name, NOT_LOADED)
                    if val is None: continue
                    if not reverse.is_collection:
                        if val is NOT_LOADED: val = attr.load(obj)
                        if val is None: continue
                        if reverse.is_required:
                            raise ConstraintError('Cannot delete %s: Attribute %s.%s for %s cannot be set to None'
                                                  % (obj, reverse.entity.__name__, reverse.name, val))
                        reverse.__set__(val, None, undo_funcs)
                    elif isinstance(reverse, Set):
                        if val is NOT_LOADED: pass
                        else: reverse.reverse_remove((val,), obj, undo_funcs)
                    else: raise NotImplementedError
                elif isinstance(attr, Set):
                    if reverse.is_required and attr.__get__(obj).__nonzero__(): raise ConstraintError(
                        'Cannot delete %s: Attribute %s.%s for associated objects cannot be set to None'
                        % (obj, reverse.entity.__name__, reverse.name))
                    attr.__set__(obj, (), undo_funcs)
                else: raise NotImplementedError

            for attr in obj._simple_keys_:
                val = get_curr(attr.name, NOT_LOADED)
                if val is NOT_LOADED: continue
                if val is None and cache.ignore_none: continue
                index = cache.indexes.get(attr)
                if index is None: continue
                obj2 = index.pop(val)
                assert obj2 is obj
                undo_list.append((index, val))
                
            for attrs in obj._composite_keys_:
                vals = tuple(get_curr(a.name, NOT_LOADED) for a in attrs)
                if NOT_LOADED in vals: continue
                if cache.ignore_none and None in vals: continue
                index = cache.indexes.get(attrs)
                if index is None: continue
                obj2 = index.pop(vals)
                assert obj2 is obj
                undo_list.append((index, vals))

            if status == 'created':
                obj._status_ = 'cancelled'
                assert obj in cache.created
                cache.created.remove(obj)
            else:
                if status == 'updated': cache.updated.remove(obj)
                elif status in ('loaded', 'saved'): cache.to_be_checked.append(obj)
                else: assert status == 'locked'
                obj._status_ = 'deleted'
                cache.deleted.add(obj)
            for attr in obj._attrs_:
                if attr.pk_offset is None:
                    val = obj._curr_.pop(attr.name, NOT_LOADED)
                    if val is NOT_LOADED: continue
                    undo_dict[attr] = val
        except:
            if not is_recursive_call:
                for undo_func in reversed(undo_funcs): undo_func()
            raise
    def delete(obj):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        obj._delete_()
    def set(obj, **keyargs):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        avdict, collection_avdict = obj._keyargs_to_avdicts_(keyargs)
        cache = obj._cache_
        status = obj._status_
        wbits = obj._wbits_
        get_curr = obj._curr_.get
        if avdict:
            for attr in avdict:
                curr = get_curr(attr.name, NOT_LOADED)
                if curr is NOT_LOADED and attr.reverse and not attr.reverse.is_collection:
                    attr.load(obj)
            if wbits is not None:
                new_wbits = wbits
                for attr in avdict: new_wbits |= obj._bits_[attr]
                obj._wbits_ = new_wbits
                if status != 'updated':
                    obj._status_ = 'updated'
                    cache.updated.add(obj)
                    if status in ('loaded', 'saved'): cache.to_be_checked.append(obj)
                    else: assert status == 'locked'
            if not collection_avdict:
                for attr in avdict:
                    if attr.reverse or attr.is_indexed: break
                else:
                    obj._curr_.update((attr.name, val) for attr, val in avdict.iteritems())
                    return
        undo_funcs = []
        undo = []
        def undo_func():
            obj._status_ = status
            obj._wbits_ = wbits
            if wbits == 0: cache.updated.remove(obj)
            if status in ('loaded', 'saved'):
                to_be_checked = cache.to_be_checked
                if to_be_checked and to_be_checked[-1] is obj: to_be_checked.pop()
                assert obj not in to_be_checked
            for index, old_key, new_key in undo:
                if new_key is NO_UNDO_NEEDED: pass
                else: del index[new_key]
                if old_key is NO_UNDO_NEEDED: pass
                else: index[old_key] = obj
        NOT_FOUND = object()
        try:
            for attr in obj._simple_keys_:
                val = avdict.get(attr, NOT_FOUND)
                if val is NOT_FOUND: continue
                curr = get_curr(attr.name, NOT_LOADED)
                if curr == val: continue
                cache.update_simple_index(obj, attr, curr, val, undo)
            for attrs in obj._composite_keys_:
                for attr in attrs:
                    if attr in avdict: break
                else: continue
                vals = [ get_curr(a.name, NOT_LOADED) for a in attrs ]
                currents = tuple(vals)
                for i, attr in enumerate(attrs):
                    val = avdict.get(attr, NOT_FOUND)
                    if val is NOT_FOUND: continue
                    vals[i] = val
                vals = tuple(vals)
                cache.update_composite_index(obj, attrs, currents, vals, undo)
            for attr, val in avdict.iteritems():
                if not attr.reverse: continue
                curr = get_curr(attr.name, NOT_LOADED)
                attr.update_reverse(obj, curr, val, undo_funcs)
            for attr, val in collection_avdict.iteritems():
                attr.__set__(obj, val, undo_funcs)
        except:
            for undo_func in undo_funcs: undo_func()
            raise
        obj._curr_.update((attr.name, val) for attr, val in avdict.iteritems())
    @classmethod
    def _normalize_args_(entity, args, keyargs, setdefault=False):
        if not args: pass
        elif len(args) != len(entity._pk_attrs_): raise TypeError('Invalid count of attrs in primary key')
        else:
            for attr, val in izip(entity._pk_attrs_, args):
                if keyargs.setdefault(attr.name, val) is not val:
                    raise TypeError('Ambiguos value of attribute %s' % attr.name)
        avdict = {}
        if setdefault:
            for name in ifilterfalse(entity._adict_.__contains__, keyargs):
                raise TypeError('Unknown attribute %r' % name)
            for attr in entity._attrs_:
                val = keyargs.get(attr.name, DEFAULT)
                avdict[attr] = attr.check(val, None, entity, from_db=False)
        else:
            get = entity._adict_.get 
            for name, val in keyargs.items():
                attr = get(name)
                if attr is None: raise TypeError('Unknown attribute %r' % name)
                avdict[attr] = attr.check(val, None, entity, from_db=False)
        if entity._pk_is_composite_:
            pkval = map(avdict.get, entity._pk_attrs_)
            if None in pkval: pkval = None
            else: pkval = tuple(pkval)
        else: pkval = avdict.get(entity._pk_)
        return pkval, avdict        
    def _keyargs_to_avdicts_(obj, keyargs):
        avdict, collection_avdict = {}, {}
        get = obj._adict_.get
        for name, val in keyargs.items():
            attr = get(name)
            if attr is None: raise TypeError('Unknown attribute %r' % name)
            val = attr.check(val, obj, from_db=False)
            if not attr.is_collection:
                if attr.pk_offset is not None:
                    curr = obj._curr_.get(attr.name, NOT_LOADED)
                    if curr != val: raise TypeError('Cannot change value of primary key attribute %s' % attr.name)
                else: avdict[attr] = val
            else: collection_avdict[attr] = val
        return avdict, collection_avdict
    def check_on_commit(obj):
        if obj._status_ not in ('loaded', 'saved'): return
        obj._status_ = 'locked'
        obj._cache_.to_be_checked.append(obj)
    @classmethod
    def _attrs_with_bit_(entity, mask=-1):
        get_bit = entity._bits_.get
        for attr in entity._attrs_:
            bit = get_bit(attr)
            if bit is None: continue
            if not bit & mask: continue
            yield attr
    def _save_principal_objects_(obj, dependent_objects):
        if dependent_objects is None: dependent_objects = []
        elif obj in dependent_objects:
            chain = ' -> '.join(obj2.__class__.__name__ for obj2 in dependent_objects)
            raise UnresolvableCyclicDependency('Cannot save cyclic chain: ' + chain)
        dependent_objects.append(obj)
        status = obj._status_
        if status == 'created': attr_iter = obj._attrs_with_bit_()
        elif status == 'updated': attr_iter = obj._attrs_with_bit_(obj._wbits_)
        else: assert False
        for attr in attr_iter:
            val = obj._curr_[attr.name]
            if not attr.reverse: continue
            if val is None: continue
            if val._status_ == 'created':
                val._save_(dependent_objects)
                assert val._status_ == 'saved'
    def _save_created_(obj):
        values = []
        for attr in obj._attrs_:
            if not attr.columns: continue
            if attr.is_collection: continue
            val = obj._curr_[attr.name]
            values.extend(attr.get_raw_values(val))
        database = obj._diagram_.database
        if obj._cached_create_sql_ is None:
            columns = obj._columns_
            converters = obj._converters_
            assert len(columns) == len(converters)
            params = [ [ PARAM, i,  converter ] for i, converter in enumerate(converters) ]
            sql_ast = [ INSERT, obj._table_, columns, params ]
            sql, adapter = database._ast2sql(sql_ast)
            obj.__class__._cached_create_sql_ = sql, adapter
        else: sql, adapter = obj._cached_create_sql_
        arguments = adapter(values)
        try:
            cursor = database._exec_sql(sql, arguments)
        except IntegrityError, e:
            raise TransactionIntegrityError(
                'Object %r cannot be stored in the database (probably it already exists). DB message: %s' % (obj, e.args[0]))
        except database.DatabaseError, e:
            raise UnexpectedError('Object %r cannot be stored in the database. DB message: %s' % (obj, e.args[0]))

        if obj._pkval_ is None:
            rowid = cursor.lastrowid # TODO
            pk_attr = obj.__class__._pk_
            index = obj._cache_.indexes.setdefault(pk_attr, {})
            obj2 = index.setdefault(rowid, obj)
            if obj2 is not obj: raise TransactionIntegrityError(
                'Newly auto-generated rowid value %s was already used in transaction cache for another object' % rowid)
            obj._pkval_ = obj._curr_[pk_attr.name] = rowid
            obj._newid_ = None
            
        obj._status_ = 'saved'
        obj._rbits_ = obj._all_bits_
        obj._wbits_ = 0
        bits = obj._bits_
        for attr in obj._attrs_:
            if attr not in bits: continue
            obj._prev_[attr.name] = obj._curr_[attr.name]
    def _save_updated_(obj):
        update_columns = []
        values = []
        for attr in obj._attrs_with_bit_(obj._wbits_):
            if not attr.columns: continue
            update_columns.extend(attr.columns)
            val = obj._curr_[attr.name]
            values.extend(attr.get_raw_values(val))
        for attr in obj._pk_attrs_:
            val = obj._curr_[attr.name]
            values.extend(attr.get_raw_values(val))
        optimistic_check_columns = []
        optimistic_check_converters = []
        if obj._cache_.optimistic:
            for attr in obj._attrs_with_bit_(obj._rbits_):
                if not attr.columns: continue
                prev = obj._prev_.get(attr.name, NOT_LOADED)
                assert prev is not NOT_LOADED
                optimistic_check_columns.extend(attr.columns)
                optimistic_check_converters.extend(attr.converters)
                values.extend(attr.get_raw_values(prev))
        query_key = (tuple(update_columns), tuple(optimistic_check_columns))
        database = obj._diagram_.database
        cached_sql = obj._update_sql_cache_.get(query_key)
        if cached_sql is None:
            update_converters = []
            for attr in obj._attrs_with_bit_(obj._wbits_):
                if not attr.columns: continue
                update_converters.extend(attr.converters)
            assert len(update_columns) == len(update_converters)
            update_params = [ [ PARAM, i, converter ] for i, converter in enumerate(update_converters) ]
            params_count = len(update_params)
            criteria_list = [ AND ]
            pk_columns = obj._pk_columns_
            pk_converters = obj._pk_converters_
            params_count = populate_criteria_list(criteria_list, pk_columns, pk_converters, params_count)
            populate_criteria_list(criteria_list, optimistic_check_columns, optimistic_check_converters, params_count)
            sql_ast = [ UPDATE, obj._table_, zip(update_columns, update_params), [ WHERE, criteria_list ] ]
            sql, adapter = database._ast2sql(sql_ast)
            obj._update_sql_cache_[query_key] = sql, adapter
        else: sql, adapter = cached_sql
        arguments = adapter(values)
        cursor = database._exec_sql(sql, arguments)
        if cursor.rowcount != 1:
            raise UnrepeatableReadError('Object %r was updated outside of current transaction' % obj)
        obj._status_ = 'saved'
        obj._rbits_ |= obj._wbits_
        obj._wbits_ = 0
        for attr in obj._attrs_with_bit_():
            val = obj._curr_.get(attr.name, NOT_LOADED)
            if val is NOT_LOADED: assert attr.name not in obj._prev_
            else: obj._prev_[attr.name] = val
    def _save_locked_(obj):
        assert obj._wbits_ == 0
        if not obj._cache_.optimistic:
            obj._status_ = 'loaded'
            return
        values = []
        for attr in obj._pk_attrs_:
            val = obj._curr_[attr.name]
            values.extend(attr.get_raw_values(val))
        optimistic_check_columns = []
        optimistic_check_converters = []
        for attr in obj._attrs_with_bit_(obj._rbits_):
            if not attr.columns: continue
            prev = obj._prev_.get(attr.name, NOT_LOADED)
            assert prev is not NOT_LOADED
            optimistic_check_columns.extend(attr.columns)
            optimistic_check_converters.extend(attr.converters)
            values.extend(attr.get_raw_values(prev))
        query_key = tuple(optimistic_check_columns)
        database = obj._diagram_.database
        cached_sql = obj._lock_sql_cache_.get(query_key)        
        if cached_sql is None:
            criteria_list = [ AND ]
            params_count = populate_criteria_list(criteria_list, obj._pk_columns_, obj._pk_converters_)
            populate_criteria_list(criteria_list, optimistic_check_columns, optimistic_check_converters, params_count)
            sql_ast = [ SELECT, [ ALL, [ VALUE, 1 ]], [ FROM, [ None, TABLE, obj._table_ ] ], [ WHERE, criteria_list ] ]
            sql, adapter = database._ast2sql(sql_ast)
            obj._lock_sql_cache_[query_key] = sql, adapter
        else: sql, adapter = cached_sql
        arguments = adapter(values)
        cursor = database._exec_sql(sql, arguments)
        row = cursor.fetchone()
        if row is None: raise UnrepeatableReadError('Object %r was updated outside of current transaction' % obj)
        obj._status_ = 'loaded'
    def _save_deleted_(obj):
        database = obj._diagram_.database
        cached_sql = obj._cached_delete_sql_
        if cached_sql is None:
            criteria_list = [ AND ]
            populate_criteria_list(criteria_list, obj._pk_columns_, obj._pk_converters_)
            sql_ast = [ DELETE, obj._table_, [ WHERE, criteria_list ] ]
            sql, adapter = database._ast2sql(sql_ast)
            obj.__class__._cached_delete_sql_ = sql, adapter
        else: sql, adapter = cached_sql
        values = obj._get_raw_pkval_()
        arguments = adapter(values)
        database._exec_sql(sql, arguments)
    def _save_(obj, dependent_objects=None):
        status = obj._status_
        if status in ('loaded', 'saved', 'cancelled'): return
        if status in ('created', 'updated'):
            obj._save_principal_objects_(dependent_objects)

        if status == 'created': obj._save_created_()
        elif status == 'updated': obj._save_updated_()
        elif status == 'deleted': obj._save_deleted_()
        elif status == 'locked': obj._save_locked_()
        else: assert False
    @classmethod
    def _get_propagation_mixin_(entity):
        mixin = entity._propagation_mixin_
        if mixin is not None: return mixin
        cls_dict = { '_entity_' : entity }
        for attr in entity._attrs_:
            if not attr.reverse:
                def fget(wrapper, attr=attr):
                    return set(attr.__get__(item) for item in wrapper)
            elif not attr.is_collection:
                def fget(wrapper, attr=attr):
                    rentity = attr.py_type
                    cls = rentity._get_propagated_set_subclass_()
                    return cls(attr.__get__(item) for item in wrapper)
            else:
                def fget(wrapper, attr=attr):
                    rentity = attr.py_type
                    cls = rentity._get_propagated_set_subclass_()
                    result_items = set()
                    for item in wrapper:
                        result_items.update(attr.__get__(item))
                    return cls(result_items)
            cls_dict[attr.name] = property(fget)
        result_cls_name = entity.__name__ + 'SetMixin'
        result_cls = type(result_cls_name, (object,), cls_dict)
        entity._propagation_mixin_ = result_cls
        return result_cls
    @classmethod
    def _get_propagated_set_subclass_(entity):
        result_cls = entity._propagated_set_subclass_
        if result_cls is None:
            mixin = entity._get_propagation_mixin_()
            cls_name = entity.__name__ + 'PropagatedSet'
            result_cls = type(cls_name, (PropagatedSet, mixin), {})
            entity._propagated_set_subclass_ = result_cls
        return result_cls
    @classmethod
    def _get_set_wrapper_subclass_(entity):
        result_cls = entity._set_wrapper_subclass_
        if result_cls is None:
            mixin = entity._get_propagation_mixin_()
            cls_name = entity.__name__ + 'SetWrapper'
            result_cls = type(cls_name, (SetWrapper, mixin), {})
            entity._set_wrapper_subclass_ = result_cls
        return result_cls

class Diagram(object):
    def __init__(diagram):
        diagram.entities = {}
        diagram.unmapped_attrs = {}
        diagram.schema = None
        diagram.database = None
    def generate_mapping(diagram, database, filename=None, check_tables=False, create_tables=False):
        if create_tables and check_tables: raise TypeError(
            "Parameters 'check_tables' and 'create_tables' cannot be set to True at the same time")

        def get_columns(table, column_names):
            return tuple(map(table.column_dict.__getitem__, column_names))

        diagram.database = database
        if diagram.schema: raise MappingError('Mapping was already generated')
        if filename is not None: raise NotImplementedError
        for entity_name in diagram.unmapped_attrs:
            raise DiagramError('Entity definition %s was not found' % entity_name)

        schema = diagram.schema = database.provider.create_schema(database)
        foreign_keys = []
        entities = list(sorted(diagram.entities.values(), key=attrgetter('_id_')))
        for entity in entities:
            entity._get_pk_columns_()
            table_name = entity._table_
            if table_name is None: table_name = entity._table_ = entity.__name__
            else: assert isinstance(table_name, basestring)
            table = schema.tables.get(table_name)
            if table is None: table = schema.add_table(table_name)
            elif table.entities: raise NotImplementedError
            table.entities.add(entity)

            if entity._base_attrs_: raise NotImplementedError
            for attr in entity._new_attrs_:
                if attr.is_collection:
                    if not isinstance(attr, Set): raise NotImplementedError
                    reverse = attr.reverse
                    if not reverse.is_collection: # many-to-one:
                        if attr.table is not None: raise MappingError(
                            "Parameter 'table' is not allowed for many-to-one attribute %s" % attr)
                        elif attr.columns: raise NotImplementedError(
                            "Parameter 'column' is not allowed for many-to-one attribute %s" % attr)
                        continue
                    # many-to-many:
                    if not isinstance(reverse, Set): raise NotImplementedError
                    if attr.entity.__name__ >= reverse.entity.__name__: continue
                    if attr.table:
                        if reverse.table != attr.table: raise MappingError(
                            "Parameter 'table' for %s and %s do not match" % (attr, reverse))
                        table_name = attr.table
                    else:
                        table_name = attr.entity.__name__ + '_' + reverse.entity.__name__
                        attr.table = reverse.table = table_name
                    m2m_table = schema.tables.get(table_name)
                    if m2m_table is not None:
                        if m2m_table.entities or m2m_table.m2m: raise MappingError(
                            "Table name '%s' is already in use" % table_name)
                        raise NotImplementedError
                    m2m_table = schema.add_table(table_name)
                    m2m_columns_1 = attr.get_m2m_columns()
                    m2m_columns_2 = reverse.get_m2m_columns()
                    assert len(m2m_columns_1) == len(reverse.converters)
                    assert len(m2m_columns_2) == len(attr.converters)
                    for column_name, converter in zip(m2m_columns_1 + m2m_columns_2, reverse.converters + attr.converters):
                        m2m_table.add_column(column_name, converter.sql_type(), True)
                    m2m_table.add_index(None, tuple(m2m_table.column_list), is_pk=True)
                    m2m_table.m2m.add(attr)
                    m2m_table.m2m.add(reverse)
                else:
                    columns = attr.get_columns()
                    if not attr.reverse and attr.default is not None:
                        assert len(attr.converters) == 1
                        if not callable(attr.default):
                            attr.converters[0].validate(attr.default)
                    assert len(columns) == len(attr.converters)
                    for (column_name, converter) in zip(columns, attr.converters):
                        table.add_column(column_name, converter.sql_type(), attr.is_required)
            if len(entity._pk_columns_) == 1 and entity._pk_.auto: is_pk = "auto"
            else: is_pk = True
            table.add_index(None, get_columns(table, entity._pk_columns_), is_pk)
            for key in entity._keys_:
                column_names = []
                for attr in key: column_names.extend(attr.columns)
                table.add_index(None, get_columns(table, column_names), is_unique=True)
            columns = []
            converters = []
            for attr in entity._attrs_:
                if attr.is_collection: continue
                columns.extend(attr.columns)  # todo: inheritance
                converters.extend(attr.converters)
            entity._columns_ = columns
            entity._converters_ = converters
        for entity in entities:
            table = schema.tables[entity._table_]
            for attr in entity._new_attrs_:
                if attr.is_collection:
                    reverse = attr.reverse
                    if not reverse.is_collection: continue
                    if not isinstance(attr, Set): raise NotImplementedError
                    if not isinstance(reverse, Set): raise NotImplementedError
                    m2m_table = schema.tables[attr.table]
                    parent_columns = get_columns(table, entity._pk_columns_)
                    child_columns = get_columns(m2m_table, reverse.columns)
                    m2m_table.add_foreign_key(None, child_columns, table, parent_columns)
                elif attr.reverse and attr.columns:
                    rentity = attr.reverse.entity
                    parent_table = schema.tables[rentity._table_]
                    parent_columns = get_columns(parent_table, rentity._pk_columns_)
                    child_columns = get_columns(table, attr.columns)
                    table.add_foreign_key(None, child_columns, parent_table, parent_columns)

        if create_tables:
            commands = schema.get_create_commands()
            database._commit_commands(commands)
            
        if not check_tables and not create_tables: return
        for table in schema.tables.values():
            sql_ast = [ SELECT,
                        [ ALL, ] + [ [ COLUMN, table.name, column.name ] for column in table.column_list ],
                        [ FROM, [ table.name, TABLE, table.name ] ],
                        [ WHERE, [ EQ, [ VALUE, 0 ], [ VALUE, 1 ] ] ]
                      ]
            sql, adapter = database._ast2sql(sql_ast)
            database._exec_sql(sql)

class Cache(object):
    def __init__(cache, database, connection):
        cache.is_alive = True
        cache.database = database
        cache.connection = connection
        cache.num = next_num()
        cache.optimistic = database.optimistic
        cache.ignore_none = True  # todo : get from provider
        cache.indexes = {}
        cache.created = set()
        cache.deleted = set()
        cache.updated = set()
        cache.modified_collections = {}
        cache.to_be_checked = []
    def flush(cache):
        assert cache.is_alive
        cache.optimistic = False
        cache.save(False)
    def commit(cache):
        assert cache.is_alive
        database = cache.database
        provider = database.provider
        connection = cache.connection
        try:
            if cache.optimistic:
                if debug: print 'OPTIMISTIC ROLLBACK'
                wrap_dbapi_exceptions(provider, connection.rollback)
        except:
            cache.is_alive = False
            cache.connection = None
            x = local.db2cache.pop(database); assert x is cache
            wrap_dbapi_exceptions(provider, database._pool.close, connection)
            raise
        save_is_needed = cache.has_anything_to_save()
        try:
            if save_is_needed: cache.save()
            if save_is_needed or not cache.optimistic:
                if debug: print 'COMMIT'
                wrap_dbapi_exceptions(provider, connection.commit)
        except:
            cache.rollback()
            raise
    def rollback(cache, close_connection=False):
        assert cache.is_alive
        database = cache.database
        x = local.db2cache.pop(database); assert x is cache
        cache.is_alive = False
        provider = database.provider
        connection = cache.connection
        cache.connection = None
        try:
            if debug: print 'ROLLBACK'    
            wrap_dbapi_exceptions(provider, connection.rollback)
            if not close_connection:
                if debug: print 'RELEASE_CONNECTION'    
                wrap_dbapi_exceptions(provider, database._pool.release, connection)
        except:
            if debug: print 'CLOSE_CONNECTION'    
            wrap_dbapi_exceptions(provider, database._pool.close, connection)
            raise
        if close_connection:
            if debug: print 'CLOSE_CONNECTION'    
            wrap_dbapi_exceptions(provider, database._pool.close, connection)
    def release(cache):
        assert cache.is_alive
        database = cache.database
        x = local.db2cache.pop(database); assert x is cache
        cache.is_alive = False
        provider = database.provider
        connection = cache.connection
        cache.connection = None
        if debug: print 'RELEASE_CONNECTION'    
        wrap_dbapi_exceptions(provider, database._pool.release, connection)
    def has_anything_to_save(cache):
        return bool(cache.created or cache.updated or cache.deleted or cache.modified_collections)                    
    def save(cache, optimistic=True):
        if not cache.has_anything_to_save(): return
        cache.optimistic = optimistic
        modified_m2m = cache.calc_modified_m2m()
        for attr, (added, removed) in modified_m2m.iteritems():
            if not removed: continue
            attr.remove_m2m(removed)
        for obj in cache.to_be_checked:
            obj._save_()
        for attr, (added, removed) in modified_m2m.iteritems():
            if not added: continue
            attr.add_m2m(added)

        cache.created.clear()
        cache.updated.clear()
        cache.deleted.clear()
        cache.modified_collections.clear()
        cache.to_be_checked[:] = []
    def calc_modified_m2m(cache):
        modified_m2m = {}
        for attr, objects in cache.modified_collections.iteritems():
            if not isinstance(attr, Set): raise NotImplementedError
            reverse = attr.reverse
            if not reverse.is_collection: continue
            if not isinstance(reverse, Set): raise NotImplementedError
            if reverse in modified_m2m: continue
            added, removed = modified_m2m.setdefault(attr, (set(), set()))
            for obj in objects:
                setdata = obj._curr_[attr.name]
                for obj2 in setdata.added: added.add((obj, obj2))
                for obj2 in setdata.removed: removed.add((obj, obj2))
        return modified_m2m
    def update_simple_index(cache, obj, attr, curr, val, undo):
        index = cache.indexes.get(attr)
        if index is None: index = cache.indexes[attr] = {}
        if val is None and cache.ignore_none: val = NO_UNDO_NEEDED
        else:
            obj2 = index.setdefault(val, obj)
            if obj2 is not obj: raise IndexError('Cannot update %s.%s: %s with key %s already exists'
                                                 % (obj.__class__.__name__, attr.name, obj2, val))
        if curr is NOT_LOADED: curr = NO_UNDO_NEEDED
        elif curr is None and cache.ignore_none: curr = NO_UNDO_NEEDED
        else: del index[curr]
        undo.append((index, curr, val))
    def db_update_simple_index(cache, obj, attr, curr, val):
        index = cache.indexes.get(attr)
        if index is None: index = cache.indexes[attr] = {}
        if val is None or cache.ignore_none: pass
        else:
            obj2 = index.setdefault(val, obj)
            if obj2 is not obj: raise TransactionIntegrityError(
                '%s with unique index %s.%s already exists: %s'
                % (obj2.__class__.__name__, obj.__class__.__name__, attr.name, new_keyval))
                # attribute which was created or updated lately clashes with one stored in database
        index.pop(curr, None)
    def update_composite_index(cache, obj, attrs, currents, vals, undo):
        if cache.ignore_none:
            if None in currents: currents = NO_UNDO_NEEDED
            if None in vals: vals = NO_UNDO_NEEDED
        if currents is NO_UNDO_NEEDED: pass
        elif NOT_LOADED in currents: currents = NO_UNDO_NEEDED
        if vals is NO_UNDO_NEEDED: pass
        elif NOT_LOADED in vals: vals = NO_UNDO_NEEDED
        if currents is NO_UNDO_NEEDED and vals is NO_UNDO_NEEDED: return
        index = cache.indexes.get(attrs)
        if index is None: index = cache.indexes[attrs] = {}
        if vals is NO_UNDO_NEEDED: pass
        else:
            obj2 = index.setdefault(vals, obj)
            if obj2 is not obj:
                attr_names = ', '.join(attr.name for attr in attrs)
                raise IndexError('Cannot update %r: composite key (%s) with value %s already exists for %r'
                                 % (obj, attr_names, vals, obj2))
        if currents is NO_UNDO_NEEDED: pass
        else: del index[currents]
        undo.append((index, currents, vals))
    def db_update_composite_index(cache, obj, attrs, currents, vals):
        index = cache.indexes.get(attrs)
        if index is None: index = cache.indexes[attrs] = {}
        if NOT_LOADED in vals: pass
        elif None in vals and cache.ignore_none: pass
        else:
            obj2 = index.setdefault(vals, obj)
            if obj2 is not obj:
                key_str = ', '.join(repr(item) for item in new_keyval)
                raise TransactionIntegrityError('%s with unique index %s.%s already exists: %s'
                                                % (obj2.__class__.__name__, obj.__class__.__name__, attr.name, key_str))
        index.pop(currents, None)

def _get_caches():
    return list(sorted((cache for cache in local.db2cache.values()),
                       reverse=True, key=lambda cache : (cache.database.priority, cache.num)))

def flush():
    for cache in _get_caches(): cache.flush()
        
def commit():
    caches = _get_caches()
    if not caches: return
    primary_cache = caches[0]
    other_caches = caches[1:]
    exceptions = []
    try:
        try: primary_cache.commit()
        except:
            exceptions.append(sys.exc_info())
            for cache in other_caches:
                try: cache.rollback()
                except: exceptions.append(sys.exc_info())
            reraise(CommitException, exceptions)
        for cache in other_caches:
            try: cache.commit()
            except: exceptions.append(sys.exc_info())
        if exceptions:
            reraise(PartialCommitException, exceptions)
    finally:
        del exceptions
        
def rollback():
    exceptions = []
    try:
        for cache in _get_caches():
            try: cache.rollback()
            except: exceptions.append(sys.exc_info())
        if exceptions:
            reraise(RollbackException, exceptions)
        assert not local.db2cache
    finally:
        del exceptions

def _release():
    for cache in _get_caches(): cache.release()
    assert not local.db2cache

def _with_transaction(func, args, keyargs, allowed_exceptions=[]):
    try:
        try: result = func(*args, **keyargs)
        except Exception, e:
            exc_info = sys.exc_info()
            try:
                # write to log
                for exc_class in allowed_exceptions:
                    if isinstance(e, exc_class):
                        commit()
                        break
                else: rollback()
            finally:
                try:
                    raise exc_info[0], exc_info[1], exc_info[2]
                finally: del exc_info
        commit()
        return result
    finally: _release()

@decorator_with_params
def with_transaction(func, retry=1, retry_exceptions=[ TransactionError ], allowed_exceptions=[]):
    def new_func(*args, **keyargs):
        counter = retry
        while counter > 0:
            try: return _with_transaction(func, args, keyargs, allowed_exceptions)
            except Exception, e:
                for exc_class in retry_exceptions:
                    if isinstance(e, exc_class): break # for
                else: raise                    
            counter -= 1
    return new_func

@simple_decorator
def db_decorator(func, *args, **keyargs):
    web = sys.modules.get('pony.web')
    allowed_exceptions = web and [ web.HttpRedirect ] or []
    try: return _with_transaction(func, args, keyargs, allowed_exceptions)
    except (ObjectNotFound, RowNotFound):
        if web: raise web.Http404NotFound
        raise

###############################################################################

MAX_ALIAS_LENGTH = 30

python_ast_cache = {}
sql_cache = {}

def select(gen):
    tree, external_names = decompile(gen)
    globals = gen.gi_frame.f_globals
    locals = gen.gi_frame.f_locals
    code = gen.gi_frame.f_code
    return Query(code, tree.code, external_names, globals, locals)

select.sum = lambda gen : select(gen).sum()
select.min = lambda gen : select(gen).min()
select.max = lambda gen : select(gen).max()
select.count = lambda gen : select(gen).count()

def exists(subquery):
    raise TypeError('Function exists() can be used inside query only')

class QueryResult(list):
    def all(self):
        return self
    def get(self):
        if not self: return None
        if len(self) > 1: raise MultipleObjectsFoundError('Multiple objects was found. Use .all(...) to retrieve them')
        return self[0]

class Query(object):
    def __init__(query, code, tree, external_names, globals, locals):
        assert isinstance(tree, ast.GenExprInner)
        query._tree = tree
        query._external_names = external_names

        query._entities = entities = {}
        query._variables = variables = {}
        query._vartypes = vartypes = {}
        query._functions = functions = {}

        for name in external_names:
            try: value = locals[name]
            except KeyError:
                try: value = globals[name]
                except KeyError:
                    try: value = getattr(__builtin__, name)
                    except AttributeError: raise NameError, name
            if value in special_functions: functions[name] = value
            elif type(value) in (types.FunctionType, types.BuiltinFunctionType):
                raise TypeError('Function %r cannot be used inside query' % value.__name__)
            elif type(value) is types.MethodType:
                raise TypeError('Method %r cannot be used inside query' % value.__name__)
            elif isinstance(value, EntityMeta):
                entities[name] = value
            elif isinstance(value, EntityIter):
                entities[name] = value.entity
            else:
                variables[name] = value
                vartypes[name] = normalize_type(type(value))

        query._result = None
        key = id(code), tuple(sorted(entities.iteritems())), \
                        tuple(sorted(vartypes.iteritems())), \
                        tuple(sorted(functions.iteritems()))
        query._python_ast_key = key
        translator = python_ast_cache.get(key)
        if translator is None:
            translator = SQLTranslator(tree, entities, vartypes, functions)
            python_ast_cache[key] = translator
        query._translator = translator
        query._database = translator.entity._diagram_.database
        query._order = query.range = None
        query._aggr_func = query._aggr_select = None
    def _construct_sql(query, range):
        translator = query._translator
        sql_key = query._python_ast_key + (query._order, range, query._aggr_func)
        cache_entry = sql_cache.get(sql_key)
        database = query._database
        if cache_entry is None:
            sql_ast = [ SELECT ]
            if query._aggr_func: sql_ast.append(query._aggr_select)
            else: sql_ast.append(translator.select)
            sql_ast.append(translator.from_)
            if translator.where: sql_ast.append(translator.where)
            if query._order:
                alias = translator.alias
                orderby_section = [ ORDER_BY ]
                for attr, asc in query._order:
                    for column in attr.columns:
                        orderby_section.append(([COLUMN, alias, column], asc and ASC or DESC))
                sql_ast = sql_ast + [ orderby_section ]
            if range:
                start, stop = range
                limit = stop - start
                offset = start
                assert limit is not None
                limit_section = [ LIMIT, [ VALUE, limit ]]
                if offset: limit_section.append([ VALUE, offset ])
                sql_ast = sql_ast + [ limit_section ]
            cache = database._get_cache()
            sql, adapter = database.provider.ast2sql(cache.connection, sql_ast)
            cache_entry = sql, adapter
            sql_cache[sql_key] = cache_entry
        else: sql, adapter = cache_entry
        return sql, adapter
    def _exec_sql(query, range):
        sql, adapter = query._construct_sql(range)
        param_dict = {}
        for param_name, extractor in query._translator.extractors.items():
            param_dict[param_name] = extractor(query._variables)
        arguments = adapter(param_dict)
        cursor = query._database._exec_sql(sql, arguments)
        return cursor
    def _fetch(query, range):
        translator = query._translator
        cursor = query._exec_sql(range)
        result = translator.entity._fetch_objects(cursor, translator.attr_offsets)
        if translator.attrname is None: return QueryResult(result)
        return QueryResult(map(attrgetter(translator.attrname), result))
    def all(query):
        return query._fetch(None)
    def get(query):
        objects = query[:2]
        if not objects: return None
        if len(objects) > 1: raise MultipleObjectsFoundError(
            'Multiple objects was found. Use select(..).all() to retrieve them')
        return objects[0]
    def __iter__(query):
        return iter(query._fetch(None))
    def orderby(query, *args):
        if not args: raise TypeError('query.orderby() requires at least one argument')
        entity = query._translator.entity
        order = []
        for arg in args:
            if isinstance(arg, Attribute): order.append((arg, True))
            elif isinstance(arg, DescWrapper): order.append((arg.attr, False))
            else: raise TypeError('query.orderby() arguments must be attributes. Got: %r' % arg)
            attr = order[-1][0]
            if entity._adict_.get(attr.name) is not attr: raise TypeError(
                'Attribute %s does not belong to Entity %s' % (attr, entity.__name__))
        new_query = object.__new__(Query)
        new_query.__dict__.update(query.__dict__)
        new_query._order = tuple(order)
        return new_query
    def __getitem__(query, key):
        if isinstance(key, slice):
            step = key.step
            if step is not None and step <> 1: raise TypeError("Parameter 'step' of slice object is not allowed here")
            start = key.start
            if start is None: start = 0
            elif start < 0: raise TypeError("Parameter 'start' of slice object cannot be negative")
            stop = key.stop
            if stop is None:
                if not start: return query.all()
                elif not query.range: raise TypeError("Parameter 'stop' of slice object should be specified")
                else: stop = query.range[1]
        else:
            try: i = key.__index__()
            except AttributeError:
                try: i = key.__int__()
                except AttributeError: raise TypeError('Incorrect argument type: %r' % key)
            result = query._fetch((i, i+1))
            return result[0]
        if start >= stop: return []
        return query._fetch((start, stop))
    def limit(query, limit, offset=None):
        start = offset or 0
        stop = start + limit
        return query[start:stop]
    def _aggregate(query, funcsymbol):
        translator = query._translator
        attrname = translator.attrname
        if attrname is not None:
            attr = translator.entity._adict_[attrname]
            attr_type = normalize_type(attr.py_type)
            if funcsymbol is SUM and attr_type not in numeric_types:
                raise TranslationError('sum is valid for numeric attributes only')
        elif funcsymbol is not COUNT: raise TranslationError(
            'Attribute should be specified for "%s" aggregate function' % funcsymbol.lower())
        query._aggr_func = funcsymbol
        column_ast = [ COLUMN, translator.alias, attr.column ]
        if funcsymbol is COUNT:
            if attrname is None: aggr_ast = [ COUNT, ALL ]
            else: aggr_ast = [ COUNT, DISTINCT, column_ast ]
        elif funcsymbol is SUM: aggr_ast = [ COALESCE, [ SUM, column_ast ], [ VALUE, 0 ] ]
        else: aggr_ast = [ funcsymbol, column_ast ]
        query._aggr_select = [ AGGREGATES, aggr_ast ]
        cursor = query._exec_sql(None)
        row = cursor.fetchone()
        if row is not None: result = row[0]
        else: result = None
        if result is None:
            if funcsymbol in (SUM, COUNT): result = 0
            else: return None
        if funcsymbol is COUNT: return result
        converter = attr.converters[0]
        return converter.sql2py(result)
    def sum(query):
        return query._aggregate(SUM)
    def min(query):
        return query._aggregate(MIN)
    def max(query):
        return query._aggregate(MAX)
    def count(query):
        return query._aggregate(COUNT)

numeric_types = set([ int, float, Decimal ])
string_types = set([ str, unicode ])
comparable_types = set([ int, float, Decimal, str, unicode, date, datetime, bool ])
primitive_types = set([ int, float, Decimal, str, unicode, date, datetime, bool, buffer ])

type_normalization_dict = { long : int, bool : int, LongStr : str, LongUnicode : unicode }

def normalize_type(t):
    if t is NoneType: return t
    if issubclass(t, basestring):  # Mainly for Html -> unicode & StrHtml -> str conversion
        if t in (str, unicode): return t
        if issubclass(t, str): return str
        if issubclass(t, unicode): return unicode
        assert False
    t = type_normalization_dict.get(t, t)
    if t not in primitive_types and not isinstance(t, EntityMeta): raise TypeError, t
    return t

some_comparables = set([ (int, float), (int, Decimal), (date, datetime) ])
some_comparables.update([ (t2, t1) for (t1, t2) in some_comparables ])

def are_comparable_types(op, type1, type2):
    # op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
    #         | 'in' | 'not' 'in' | 'is' | 'is' 'not'
    if op in ('is', 'is not'): return type1 is not NoneType and type2 is NoneType
    if op in ('<', '<=', '>', '>='):
        return (type1 is type2 and type1 in comparable_types) \
            or (type1, type2) in some_comparables
    if op in ('==', '<>', '!='):
        if type1 is NoneType and type2 is NoneType: return False
        if type1 is NoneType or type2 is NoneType: return True
        elif type1 in primitive_types:
            return type1 is type2 or (type1, type2) in some_comparables
        elif isinstance(type1, EntityMeta):
            if not isinstance(type2, EntityMeta): return False
            return type1._root_ is type2._root_
        else: return False
    else: assert False

def sqland(items):
    if not items: return []
    if len(items) == 1: return items[0]
    return [ AND ] + items

def sqlor(items):
    if not items: return []
    if len(items) == 1: return items[0]
    return [ OR ] + items

def join_tables(conditions, alias1, alias2, columns1, columns2):
    assert len(columns1) == len(columns2)
    conditions.extend([ EQ, [ COLUMN, alias1, c1 ], [ COLUMN, alias2, c2 ] ]
                     for c1, c2 in izip(columns1, columns2))

class ASTTranslator(object):
    def __init__(translator, tree):
        translator.tree = tree
        translator.pre_methods = {}
        translator.post_methods = {}
    def dispatch(translator, node):
        cls = node.__class__

        try: pre_method = translator.pre_methods[cls]
        except KeyError:
            pre_method = getattr(translator, 'pre' + cls.__name__, None)
            translator.pre_methods[cls] = pre_method
        if pre_method is not None:
            # print 'PRE', node.__class__.__name__, '+'
            stop = pre_method(node)
        else:            
            # print 'PRE', node.__class__.__name__, '-'
            stop = translator.default_pre(node)

        if stop: return
            
        for child in node.getChildNodes():
            translator.dispatch(child)

        try: post_method = translator.post_methods[cls]
        except KeyError:
            post_method = getattr(translator, 'post' + cls.__name__, None)
            translator.post_methods[cls] = post_method
        if post_method is not None:
            # print 'POST', node.__class__.__name__, '+'
            post_method(node)
        else:            
            # print 'POST', node.__class__.__name__, '-'
            translator.default_post(node)
    def default_pre(translator, node):
        pass
    def default_post(translator, node):
        pass

class SQLTranslator(ASTTranslator):
    def __init__(translator, tree, entities, vartypes, functions, outer_iterables={}):
        assert isinstance(tree, ast.GenExprInner), tree
        ASTTranslator.__init__(translator, tree)
        translator.diagram = None
        translator.entities = entities
        translator.vartypes = vartypes
        translator.functions = functions
        translator.outer_iterables = outer_iterables
        translator.iterables = iterables = {}
        translator.aliases = aliases = {}
        translator.extractors = {}
        translator.distinct = False
        translator.from_ = [ FROM ]
        conditions = translator.conditions = []
        translator.inside_expr = False
        translator.alias_counters = {}
        for i, qual in enumerate(tree.quals):
            assign = qual.assign
            if not isinstance(assign, ast.AssName): raise TypeError
            if assign.flags != 'OP_ASSIGN': raise TypeError

            name = assign.name
            if name in iterables: raise TranslationError('Duplicate name: %s' % name)
            if name.startswith('__'): raise TranslationError('Illegal name: %s' % name)
            assert name not in aliases

            node = qual.iter
            attr_names = []
            while isinstance(node, ast.Getattr):
                attr_names.append(node.attrname)
                node = node.expr
            if not isinstance(node, ast.Name): raise TypeError

            if not attr_names:
                if i > 0: translator.distinct = True
                iter_name = node.name
                entity = entities.get(iter_name)
                if entity is None:
                    if iter_name in vartypes: raise NotImplementedError
                    else: raise NameError, iter_name
                diagram = entity._diagram_
                if diagram.database is None: raise TranslationError(
                    'Entity %s is not mapped to a database' % entity.__name__)
                if translator.diagram is None: translator.diagram = diagram
                elif translator.diagram is not diagram: raise TranslationError(
                    'All entities in a query must belong to the same diagram')
            else:
                if len(attr_names) > 1: raise NotImplementedError
                attrname = attr_names[0]
                parent_entity = iterables.get(node.name) or outer_iterables.get(node.name)
                if parent_entity is None: raise TranslationError("Name %r must be defined in query" % node.name)
                attr = parent_entity._adict_.get(attrname)
                if attr is None: raise AttributeError, attrname
                if not attr.is_collection: raise TypeError
                if not isinstance(attr, Set): raise NotImplementedError
                entity = attr.py_type
                if not isinstance(entity, EntityMeta): raise NotImplementedError
                reverse = attr.reverse
                if not reverse.is_collection:
                    join_tables(conditions, node.name, name, parent_entity._pk_columns_, reverse.columns)
                else:
                    if not isinstance(reverse, Set): raise NotImplementedError
                    translator.distinct = True
                    m2m_table = attr.table
                    m2m_alias = '%s--%s' % (node.name, name)
                    aliases[m2m_alias] = m2m_alias
                    translator.from_.append([ m2m_alias, TABLE, m2m_table ])
                    join_tables(conditions, node.name, m2m_alias, parent_entity._pk_columns_, reverse.columns)
                    join_tables(conditions, m2m_alias, name, attr.columns, entity._pk_columns_)
            iterables[name] = entity
            aliases[name] = name
            translator.from_.append([ name, TABLE, entity._table_ ])
            for if_ in qual.ifs:
                assert isinstance(if_, ast.GenExprIf)
                translator.dispatch(if_)
                translator.conditions.append(if_.monad.getsql())
        translator.inside_expr = True
        translator.dispatch(tree.expr)
        monad = tree.expr.monad
        translator.attrname = None
        if isinstance(monad, AttrMonad) and not isinstance(monad, ObjectMixin):
            translator.attrname = monad.attr.name
            monad = monad.parent
        if not isinstance(monad, ObjectMixin):
            raise NotImplementedError
        alias = monad.alias
        entity = translator.entity = monad.type
        if isinstance(monad, ObjectIterMonad):
            if alias != translator.tree.quals[-1].assign.name:
                translator.distinct = True
        elif isinstance(monad, ObjectAttrMonad):
            translator.distinct = True
            assert alias in aliases
        elif isinstance(monad, ObjectFlatMonad): pass
        else: assert False
        short_alias = translator.alias = aliases[alias]
        translator.select, translator.attr_offsets = entity._construct_select_clause_(short_alias, translator.distinct)
        if not translator.conditions: translator.where = None
        else: translator.where = [ WHERE, sqland(translator.conditions) ]
    def preGenExpr(translator, node):
        inner_tree = node.code
        outer_iterables = {}
        outer_iterables.update(translator.outer_iterables)
        outer_iterables.update(translator.iterables)
        subtranslator = SQLTranslator(inner_tree, translator.entities, translator.vartypes, translator.functions, outer_iterables)
        node.monad = QuerySetMonad(translator, subtranslator)
        return True
    def postGenExprIf(translator, node):
        monad = node.test.monad
        if monad.type is not bool: monad = monad.nonzero()
        node.monad = monad
    def postCompare(translator, node):
        expr1 = node.expr
        ops = node.ops
        if len(ops) > 1: raise NotImplementedError
        op, expr2 = ops[0]
        # op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
        #         | 'in' | 'not in' | 'is' | 'is not'
        if op.endswith('in'):
            node.monad = expr2.monad.contains(expr1.monad, op == 'not in')
        else:
            node.monad = expr1.monad.cmp(op, expr2.monad)
    def postConst(translator, node):
        value = node.value
        if type(value) is not tuple:
            node.monad = ConstMonad(translator, value)
        else:
            node.monad = ListMonad(translator, [ ConstMonad(translator, item) for item in value ])
    def postList(translator, node):
        node.monad = ListMonad(translator, [ item.monad for item in node.nodes ])
    def postTuple(translator, node):
        node.monad = ListMonad(translator, [ item.monad for item in node.nodes ])
    def postName(translator, node):
        name = node.name
        entity = translator.iterables.get(name)
        if entity is None: entity = translator.outer_iterables.get(name)
        if entity is not None:
            node.monad = ObjectIterMonad(translator, name, entity)
            return

        value_type = translator.entities.get(name)
        if value_type is not None:
            node.monad = EntityMonad(translator, value_type)
            return
            
        try: value_type = translator.vartypes[name]
        except KeyError:
            func = translator.functions.get(name)
            if func is None: raise NameError(name)
            func_monad_class = special_functions[func]
            node.monad = func_monad_class(translator)
        else:
            if name in ('True', 'False') and issubclass(value_type, int):
                node.monad = ConstMonad(translator, name == 'True' and 1 or 0)
            elif value_type is NoneType: node.monad = ConstMonad(translator, None)
            else: node.monad = ParamMonad(translator, value_type, name)
    def postAdd(translator, node):
        node.monad = node.left.monad + node.right.monad
    def postSub(translator, node):
        node.monad = node.left.monad - node.right.monad
    def postMul(translator, node):
        node.monad = node.left.monad * node.right.monad
    def postDiv(translator, node):
        node.monad = node.left.monad / node.right.monad
    def postPower(translator, node):
        node.monad = node.left.monad ** node.right.monad
    def postUnarySub(translator, node):
        node.monad = -node.expr.monad
    def postGetattr(translator, node):
        node.monad = node.expr.monad.getattr(node.attrname)
    def postAnd(translator, node):
        node.monad = AndMonad([ subnode.monad for subnode in node.nodes ])
    def postOr(translator, node):
        node.monad = OrMonad([ subnode.monad for subnode in node.nodes ])
    def postNot(translator, node):
        node.monad = node.expr.monad.negate()
    def preCallFunc(translator, node):
        if node.star_args is not None: raise NotImplementedError
        if node.dstar_args is not None: raise NotImplementedError
        if isinstance(node.node, ast.Name):
            pass

        if len(node.args) > 1: return False
        if not node.args: return False
        arg = node.args[0]
        if not isinstance(arg, ast.GenExpr): return False
        translator.dispatch(node.node)
        func_monad = node.node.monad
        translator.dispatch(arg)
        query_set_monad = arg.monad
        node.monad = func_monad(query_set_monad)
        return True
    def postCallFunc(translator, node):
        args = []
        keyargs = {}
        for arg in node.args:
            if isinstance(arg, ast.Keyword):
                keyargs[arg.name] = arg.expr.monad
            else: args.append(arg.monad)
        func_monad = node.node.monad
        node.monad = func_monad(*args, **keyargs)
    def postSubscript(translator, node):
        assert node.flags == 'OP_APPLY'
        assert isinstance(node.subs, list) and len(node.subs) == 1
        expr_monad = node.expr.monad
        index_monad = node.subs[0].monad
        node.monad = expr_monad[index_monad]
    def postSlice(translator, node):
        assert node.flags == 'OP_APPLY'
        expr_monad = node.expr.monad
        upper = node.upper
        if upper is not None: upper = upper.monad
        lower = node.lower
        if lower is not None: lower = lower.monad
        node.monad = expr_monad[lower:upper]
    def get_short_alias(translator, alias, entity_name):
        if alias and len(alias) <= MAX_ALIAS_LENGTH: return alias
        name = entity_name[:MAX_ALIAS_LENGTH-3].lower()
        i = translator.alias_counters.setdefault(name, 0) + 1
        short_alias = '%s-%d' % (name, i)
        translator.alias_counters[name] = i
        return short_alias

class Monad(object):
    def __init__(monad, translator, type):
        monad.translator = translator
        monad.type = type
        monad.mixin_init()
    def mixin_init(monad): pass
    def cmp(monad, op, monad2):
        return CmpMonad(op, monad, monad2)
    def contains(monad, item, not_in=False): raise TypeError
    def nonzero(monad): raise TypeError
    def negate(monad):
        return NotMonad(monad)

    def getattr(monad, attrname): raise TypeError
    def __call__(monad, *args, **keyargs): raise TypeError
    def len(monad): raise TypeError
    def sum(monad): raise TypeError
    def min(monad): raise TypeError
    def max(monad): raise TypeError
    def __getitem__(monad, key): raise TypeError

    def __add__(monad, monad2): raise TypeError
    def __sub__(monad, monad2): raise TypeError
    def __mul__(monad, monad2): raise TypeError
    def __div__(monad, monad2): raise TypeError
    def __pow__(monad, monad2): raise TypeError

    def __neg__(monad): raise TypeError
    def abs(monad): raise TypeError

class EntityMonad(Monad):
    def __call__(monad, *args, **keyargs):
        pkval, avdict = monad.normalize_args(args, keyargs)
        if pkval is None or len(avdict) > len(pkval): raise NotImplementedError
        return ObjectConstMonad(monad.translator, monad.type, pkval)
    def normalize_args(monad, args, keyargs):
        entity = monad.type
        if not args: pass
        elif len(args) != len(entity._pk_attrs_): raise TypeError('Invalid count of attrs in primary key')
        else:
            for attr, val_monad in izip(entity._pk_attrs_, args):
                if keyargs.setdefault(attr.name, val_monad) is not val_monad:
                    raise TypeError('Ambiguos value of attribute %s' % attr.name)
        avdict = {}
        get = entity._adict_.get 
        for name, val_monad in keyargs.items():
            val_type = val_monad.type
            attr = get(name)
            if attr is None: raise TypeError('Unknown attribute %r' % name)
            if attr.is_collection: raise NotImplementedError
            if attr.is_ref:
                if not issubclass(val_type, attr.py_type): raise TypeError
                if not isinstance(val_monad, ObjectConstMonad):
                    raise TypeError('Entity constructor arguments in declarative query should be consts')
                avdict[attr] = val_monad
            elif isinstance(val_monad, ConstMonad):
                val = val_monad.value
                avdict[attr] = attr.check(val, None, entity, from_db=False)
            else: raise TypeError('Entity constructor arguments in declarative query should be consts')
        pkval = map(avdict.get, entity._pk_attrs_)
        if None in pkval: pkval = None
        return pkval, avdict

class ListMonad(Monad):
    def __init__(monad, translator, items):
        Monad.__init__(monad, translator, list)
        monad.items = items
    def contains(monad, x, not_in=False):
        for item in monad.items:
            if not are_comparable_types('==', x.type, item.type): raise TypeError
        left_sql = x.getsql()
        if len(left_sql) == 1:
            if not_in: sql = [ NOT_IN, left_sql[0], [ item.getsql()[0] for item in monad.items ] ]
            else: sql = [ IN, left_sql[0], [ item.getsql()[0] for item in monad.items ] ]
        elif not_in:
            sql = sqland([ sqlor([ [ NE, a, b ]  for a, b in zip(left_sql, item.getsql()) ]) for item in monad.items ])
        else:
            sql = sqlor([ sqland([ [ EQ, a, b ]  for a, b in zip(left_sql, item.getsql()) ]) for item in monad.items ])
        return BoolExprMonad(monad.translator, sql)

numeric_conversions = {
    (int, float): float,
    (int, Decimal): Decimal,
    }
numeric_conversions.update(((t2, t1), t3) for (t1, t2), t3 in numeric_conversions.items())

def make_numeric_binop(sqlop):
    def numeric_binop(monad, monad2):
        if not isinstance(monad2, NumericMixin): raise TypeError
        t1, t2 = monad.type, monad2.type
        if t1 is t2: result_type = t1
        else: result_type = numeric_conversions.get((t1, t2))
        if result_type is None: raise TypeError('Unsupported combination of %s and %s' % (monad.type, monad2.type))
        left_sql = monad.getsql()
        right_sql = monad2.getsql()
        assert len(left_sql) == len(right_sql) == 1
        return NumericExprMonad(monad.translator, result_type, [ sqlop, left_sql[0], right_sql[0] ])
    numeric_binop.__name__ = sqlop
    return numeric_binop

class NumericMixin(object):
    def mixin_init(monad):
        assert monad.type in numeric_types
    __add__ = make_numeric_binop(ADD)
    __sub__ = make_numeric_binop(SUB)
    __mul__ = make_numeric_binop(MUL)
    __div__ = make_numeric_binop(DIV)
    def __pow__(monad, monad2):
        if not isinstance(monad2, NumericMixin): raise TypeError
        left_sql = monad.getsql()
        right_sql = monad2.getsql()
        assert len(left_sql) == len(right_sql) == 1
        return NumericExprMonad(monad.translator, float, [ POW, left_sql[0], right_sql[0] ])
    def __neg__(monad):
        sql = monad.getsql()[0]
        return NumericExprMonad(monad.translator, monad.type, [ NEG, sql ])
    def abs(monad):
        sql = monad.getsql()[0]
        return NumericExprMonad(monad.translator, monad.type, [ ABS, sql ])
    def nonzero(monad):
        return CmpMonad('!=', monad, ConstMonad(monad.translator, 0))
    def negate(monad):
        return CmpMonad('==', monad, ConstMonad(monad.translator, 0))

class DateMixin(object):
    def mixin_init(monad):
        assert monad.type is date

class DatetimeMixin(object):
    def mixin_init(monad):
        assert monad.type is datetime

def make_string_binop(sqlop):
    def string_binop(monad, monad2):
        if monad.type is not monad2.type: raise TypeError
        left_sql = monad.getsql()
        right_sql = monad2.getsql()
        assert len(left_sql) == len(right_sql) == 1
        return StringExprMonad(monad.translator, monad.type, [ sqlop, left_sql[0], right_sql[0] ])
    string_binop.__name__ = sqlop
    return string_binop

class StringMixin(object):
    def mixin_init(monad):
        assert issubclass(monad.type, basestring)
        monad.type = unicode
    def getattr(monad, attrname):
        return StringMethodMonad(monad.translator, monad, attrname)
    __add__ = make_string_binop(CONCAT)
    def __getitem__(monad, index):
        if isinstance(index, slice):
            if index.step is not None: raise TypeError("Slice 'step' attribute is not supported")
            start, stop = index.start, index.stop
            if start is None and stop is None: return monad
            if isinstance(monad, StringConstMonad) \
               and (start is None or isinstance(start, NumericConstMonad)) \
               and (stop is None or isinstance(stop, NumericConstMonad)):
                if start is not None: start = start.value
                if stop is not None: stop = stop.value
                return ConstMonad(monad.translator, monad.value[start:stop])

            if start is not None and start.type is not int: raise TypeError('string indices must be integers')
            if stop is not None and stop.type is not int: raise TypeError('string indices must be integers')
            
            expr_sql = monad.getsql()[0]

            if start is None: start = ConstMonad(monad.translator, 0)
            
            if isinstance(start, NumericConstMonad):
                if start.value < 0: raise NotImplementedError('Negative slice indices not supported')
                start_sql = [ VALUE, start.value + 1 ]
            else:
                start_sql = start.getsql()[0]
                start_sql = [ ADD, start_sql, [ VALUE, 1 ] ]

            if stop is None:
                len_sql = None
            elif isinstance(stop, NumericConstMonad):
                if stop.value < 0: raise NotImplementedError('Negative slice indices not supported')
                if isinstance(start, NumericConstMonad):
                    len_sql = [ VALUE, stop.value - start.value ]
                else:
                    len_sql = [ SUB, [ VALUE, stop.value ], start.getsql()[0] ]
            else:
                stop_sql = stop.getsql()[0]
                if isinstance(start, NumericConstMonad):
                    len_sql = [ SUB, stop_sql, [ VALUE, start.value ] ]
                else:
                    len_sql = [ SUB, stop_sql, start.getsql()[0] ]

            sql = [ SUBSTR, expr_sql, start_sql, len_sql ]
            return StringExprMonad(monad.translator, monad.type, sql)
        
        if isinstance(monad, StringConstMonad) and isinstance(index, NumericConstMonad):
            return ConstMonad(monad.translator, monad.value[index.value])
        if index.type is not int: raise TypeError('string indices must be integers')
        expr_sql = monad.getsql()[0]
        if isinstance(index, NumericConstMonad):
            value = index.value
            if value >= 0: value += 1
            index_sql = [ VALUE, value ]
        else:
            inner_sql = index.getsql()[0]
            index_sql = [ ADD, inner_sql, [ CASE, None, [ ([GE, inner_sql, [ VALUE, 0 ]], [ VALUE, 1 ]) ], [ VALUE, 0 ] ] ]
        sql = [ SUBSTR, expr_sql, index_sql, [ VALUE, 1 ] ]
        return StringExprMonad(monad.translator, monad.type, sql)
    def len(monad):
        sql = monad.getsql()[0]
        return NumericExprMonad(monad.translator, int, [ LENGTH, sql ])
    def contains(monad, item, not_in=False):
        if item.type is not monad.type: raise TypeError
        if isinstance(item, StringConstMonad):
            item_sql = [ VALUE, '%%%s%%' % item.value ]
        else:
            item_sql = [ CONCAT, [ VALUE, '%' ], item.getsql()[0], [ VALUE, '%' ] ]
        sql = [ LIKE, monad.getsql()[0], item_sql ]
        return BoolExprMonad(monad.translator, sql)
        
class MethodMonad(Monad):
    def __init__(monad, translator, parent, attrname):
        Monad.__init__(monad, translator, 'METHOD')
        monad.parent = parent
        monad.attrname = attrname
        try: method = getattr(monad, 'call_' + monad.attrname)
        except AttributeError:
            raise AttributeError('%r object has no attribute %r' % (parent.type.__name__, attrname))
    def __call__(monad, *args, **keyargs):
        method = getattr(monad, 'call_' + monad.attrname)
        return method(*args, **keyargs)

def make_string_func(sqlop):
    def func(monad):
        sql = monad.parent.getsql()
        assert len(sql) == 1
        return StringExprMonad(monad.translator, monad.parent.type, [ sqlop, sql[0] ])
    func.__name__ = sqlop
    return func

class StringMethodMonad(MethodMonad):
    call_upper = make_string_func(UPPER)
    call_lower = make_string_func(LOWER)
    def call_startswith(monad, arg):
        parent_sql = monad.parent.getsql()[0]
        if arg.type is not monad.parent.type:
            raise TypeError("Argument of 'startswith' method must be a string")
        if isinstance(arg, StringConstMonad):
            assert isinstance(arg.value, basestring)
            arg_sql = [ VALUE, arg.value + '%' ]
        else:
            arg_sql = arg.getsql()[0]
            arg_sql = [ CONCAT, arg_sql, [ VALUE, '%' ] ]
        sql = [ LIKE, parent_sql, arg_sql ]
        return BoolExprMonad(monad.translator, sql)
    def call_endswith(monad, arg):
        parent_sql = monad.parent.getsql()[0]
        if arg.type is not monad.parent.type:
            raise TypeError("Argument of 'endswith' method must be a string")
        if isinstance(arg, StringConstMonad):
            assert isinstance(arg.value, basestring)
            arg_sql = [ VALUE, '%' + arg.value ]
        else:
            arg_sql = arg.getsql()[0]
            arg_sql = [ CONCAT, [ VALUE, '%' ], arg_sql ]
        sql = [ LIKE, parent_sql, arg_sql ]
        return BoolExprMonad(monad.translator, sql)
    def strip(monad, chars, strip_type):
        parent_sql = monad.parent.getsql()[0]
        if chars is not None and chars.type is not monad.parent.type:
            raise TypeError("'chars' argument must be a %s" % monad.parent.type.__name__)
        if chars is None:
            return StringExprMonad(monad.translator, monad.parent.type, [ strip_type, parent_sql ])
        else:
            chars_sql = chars.getsql()[0]
            return StringExprMonad(monad.translator, monad.parent.type, [ strip_type, parent_sql, chars_sql ])
    def call_strip(monad, chars=None):
        return monad.strip(chars, TRIM)
    def call_lstrip(monad, chars=None):
        return monad.strip(chars, LTRIM)
    def call_rstrip(monad, chars=None):
        return monad.strip(chars, RTRIM)
    
class ObjectMixin(object):
    def mixin_init(monad):
        assert isinstance(monad.type, EntityMeta)
    def getattr(monad, name):
        translator = monad.translator
        entity = monad.type
        attr = getattr(entity, name) # can raise AttributeError
        if not attr.is_collection:
            return AttrMonad.new(monad, attr)
        elif not translator.inside_expr:
            return AttrSetMonad(monad, [ attr ])
        else:
            return ObjectFlatMonad(monad, attr)

class ObjectIterMonad(ObjectMixin, Monad):
    def __init__(monad, translator, alias, entity):
        Monad.__init__(monad, translator, entity)
        monad.alias = alias
    def getsql(monad):
        entity = monad.type
        return [ [ COLUMN, monad.alias, column ] for attr in entity._pk_attrs_ if not attr.is_collection
                                                 for column in attr.columns ]

class AttrMonad(Monad):
    @staticmethod
    def new(parent, attr, *args, **keyargs):
        type = normalize_type(attr.py_type)
        if type in numeric_types: cls = NumericAttrMonad
        elif type in string_types: cls = StringAttrMonad
        elif type is date: cls = DateAttrMonad
        elif type is datetime: cls = DatetimeAttrMonad
        elif type is buffer: cls = BufferAttrMonad
        elif isinstance(type, EntityMeta): cls = ObjectAttrMonad
        else: raise NotImplementedError, type
        return cls(parent, attr, *args, **keyargs)
    def __init__(monad, parent, attr):
        assert monad.__class__ is not AttrMonad
        attr_type = normalize_type(attr.py_type)
        Monad.__init__(monad, parent.translator, attr_type)
        monad.parent = parent
        monad.attr = attr
        monad.alias = None
    def getsql(monad):
        return [ [ COLUMN, monad.parent.alias, column ] for column in monad.attr.columns ]
        
class ObjectAttrMonad(ObjectMixin, AttrMonad):
    def __init__(monad, parent, attr):
        AttrMonad.__init__(monad, parent, attr)
        monad.alias = '-'.join((parent.alias, attr.name))
        monad._make_join()
    def _make_join(monad):
        translator = monad.translator
        parent = monad.parent
        attr = monad.attr
        alias = monad.alias
        entity = monad.type

        short_alias = translator.aliases.get(alias)
        if short_alias is not None: return
        short_alias = translator.get_short_alias(alias, entity.__name__)
        translator.aliases[alias] = short_alias
        translator.from_.append([ short_alias, TABLE, entity._table_ ])
        join_tables(translator.conditions, parent.alias, short_alias, attr.columns, entity._pk_columns_)

class ObjectFlatMonad(ObjectMixin, Monad):
    def __init__(monad, parent, attr):
        assert parent.translator.inside_expr
        type = normalize_type(attr.py_type)
        Monad.__init__(monad, parent.translator, type)
        monad.parent = parent
        monad.attr = attr
        monad.alias = '-'.join((parent.alias, attr.name))
        monad._make_join()
    def _make_join(monad):
        translator = monad.translator
        conditions = translator.conditions
        parent = monad.parent
        attr = monad.attr
        reverse = attr.reverse
        alias = monad.alias
        entity = monad.type
        parent_entity = monad.parent.type

        short_alias = translator.aliases.get(alias)
        assert short_alias is None
        short_alias = translator.get_short_alias(alias, entity.__name__)
        translator.aliases[alias] = short_alias
        if not reverse.is_collection:           
            translator.from_.append([ short_alias, TABLE, entity._table_ ])
            join_tables(conditions, parent.alias, short_alias, parent_entity._pk_columns_, reverse.columns)
        else:
            m2m_table = attr.table
            m2m_alias = monad.translator.get_short_alias(None, 'm2m-')
            translator.from_.append([ m2m_alias, TABLE, m2m_table ])
            join_tables(conditions, parent.alias, m2m_alias, parent_entity._pk_columns_, reverse.columns)
            translator.from_.append([ short_alias, TABLE, entity._table_ ])
            join_tables(conditions, m2m_alias, alias, attr.columns, entity._pk_columns_)
        
class NumericAttrMonad(NumericMixin, AttrMonad): pass
class StringAttrMonad(StringMixin, AttrMonad): pass
class DateAttrMonad(DateMixin, AttrMonad): pass
class DatetimeAttrMonad(DatetimeMixin, AttrMonad): pass
class BufferAttrMonad(AttrMonad): pass

class ParamMonad(Monad):
    def __new__(cls, translator, type, name, parent=None):
        assert cls is ParamMonad
        type = normalize_type(type)
        if type in numeric_types: cls = NumericParamMonad
        elif type in string_types: cls = StringParamMonad
        elif type is date: cls = DateParamMonad
        elif type is datetime: cls = DatetimeParamMonad
        elif type is buffer: cls = BufferParamMonad
        elif isinstance(type, EntityMeta): cls = ObjectParamMonad
        else: raise TypeError, type
        return object.__new__(cls)
    def __init__(monad, translator, type, name, parent=None):
        type = normalize_type(type)
        Monad.__init__(monad, translator, type)
        monad.name = name
        monad.parent = parent
        if not isinstance(type, EntityMeta):
            provider = translator.diagram.database.provider
            monad.converter = provider.get_converter_by_py_type(type)
        else: monad.converter = None
        if parent is None: monad.extractor = lambda variables : variables[name]
        else: monad.extractor = lambda variables : getattr(parent.extractor(variables), name)
    def getsql(monad):
        monad.add_extractors()
        return [ [ PARAM, monad.name, monad.converter ] ]
    def add_extractors(monad):
        name = monad.name
        extractors = monad.translator.extractors
        extractors[name] = monad.extractor

class ObjectParamMonad(ObjectMixin, ParamMonad):
    def __init__(monad, translator, entity, name, parent=None):
        if translator.diagram is not entity._diagram_: raise TranslationError(
            'All entities in a query must belong to the same diagram')
        monad.params = [ '-'.join((name, path)) for path in entity._pk_paths_ ]
        ParamMonad.__init__(monad, translator, entity, name, parent)
    def getattr(monad, name):
        entity = monad.type
        attr = entity._adict_[name]
        return ParamMonad(monad.translator, attr.py_type, name, monad)
    def getsql(monad):
        monad.add_extractors()
        entity = monad.type
        assert len(monad.params) == len(entity._pk_converters_)
        return [ [ PARAM, param, converter ] for param, converter in zip(monad.params, entity._pk_converters_) ]
    def add_extractors(monad):
        entity = monad.type
        extractors = monad.translator.extractors
        if len(entity._pk_columns_) == 1:
            extractors[monad.params[0]] = lambda vars, e=monad.extractor : e(vars)._get_raw_pkval_()[0]
        else:
            for i, param in enumerate(monad.params):
                extractors[param] = lambda vars, i=i, e=monad.extractor : e(vars)._get_raw_pkval_()[i]

class StringParamMonad(StringMixin, ParamMonad): pass
class NumericParamMonad(NumericMixin, ParamMonad): pass
class DateParamMonad(DateMixin, ParamMonad): pass
class DatetimeParamMonad(DatetimeMixin, ParamMonad): pass
class BufferParamMonad(ParamMonad): pass

class ExprMonad(Monad):
    @staticmethod
    def new(translator, type, sql):
        if type in numeric_types: cls = NumericExprMonad
        elif type in string_types: cls = StringExprMonad
        elif type is date: cls = DateExprMonad
        elif type is datetime: cls = DatetimeExprMonad
        else: raise NotImplementedError, type
        return cls(translator, type, sql)
    def __init__(monad, translator, type, sql):
        Monad.__init__(monad, translator, type)
        monad.sql = sql
    def getsql(monad):
        return [ monad.sql ]

class StringExprMonad(StringMixin, ExprMonad): pass
class NumericExprMonad(NumericMixin, ExprMonad): pass
class DateExprMonad(DateMixin, ExprMonad): pass
class DatetimeExprMonad(DatetimeMixin, ExprMonad): pass

class ConstMonad(Monad):
    def __new__(cls, translator, value):
        assert cls is ConstMonad
        value_type = normalize_type(type(value))
        if value_type in numeric_types: cls = NumericConstMonad
        elif value_type in string_types: cls = StringConstMonad
        elif value_type is date: cls = DateConstMonad
        elif value_type is datetime: cls = DatetimeConstMonad
        elif value_type is NoneType: cls = NoneMonad
        else: raise TypeError, value_type
        return object.__new__(cls)
    def __init__(monad, translator, value):
        value_type = normalize_type(type(value))
        Monad.__init__(monad, translator, value_type)
        monad.value = value
    def getsql(monad):
        return [ [ VALUE, monad.value ] ]

class NoneMonad(ConstMonad):
    type = NoneType
    def __init__(monad, translator, value=None):
        assert value is None
        ConstMonad.__init__(monad, translator, value)

class StringConstMonad(StringMixin, ConstMonad):
    def len(monad):
        return ConstMonad(monad.translator, len(monad.value))
    
class NumericConstMonad(NumericMixin, ConstMonad): pass
class DateConstMonad(DateMixin, ConstMonad): pass
class DatetimeConstMonad(DatetimeMixin, ConstMonad): pass

class ObjectConstMonad(Monad):
    def __init__(monad, translator, entity, pkval):
        Monad.__init__(monad, translator, entity)
        monad.pkval = pkval
        rawpkval = monad.rawpkval = []
        for attr, val in izip(entity._pk_attrs_, pkval):
            if attr.is_ref:
                assert isinstance(val, ObjectConstMonad)
                rawpkval.extend(val.rawpkval)
            else:
                assert not isinstance(val, Monad)
                rawpkval.append(val)
    def getsql(monad):
        entity = monad.type
        return [ [ VALUE, value ] for value in monad.rawpkval ]

class BoolMonad(Monad):
    def __init__(monad, translator):
        monad.translator = translator
        monad.type = bool

sql_negation = { IN : NOT_IN, EXISTS : NOT_EXISTS, LIKE : NOT_LIKE, BETWEEN : NOT_BETWEEN, IS_NULL : IS_NOT_NULL }
sql_negation.update((value, key) for key, value in sql_negation.items())

class BoolExprMonad(BoolMonad):
    def __init__(monad, translator, sql):
        monad.translator = translator
        monad.type = bool
        monad.sql = sql
    def getsql(monad):
        return monad.sql
    def negate(monad):
        sql = monad.sql
        sqlop = sql[0]
        negated_op = sql_negation.get(sqlop)
        if negated_op is not None:
            negated_sql = [ negated_op ] + sql[1:]
        elif negated_op == NOT:
            assert len(sql) == 2
            negated_sql = sql[1]
        else:
            return NotMonad(monad.translator, sql)
        return BoolExprMonad(monad.translator, negated_sql)

cmp_ops = { '>=' : GE, '>' : GT, '<=' : LE, '<' : LT }        

cmp_negate = { '<' : '>=', '<=' : '>', '==' : '!=', 'is' : 'is not' }
cmp_negate.update((b, a) for a, b in cmp_negate.items())

class CmpMonad(BoolMonad):
    def __init__(monad, op, left, right):
        if not are_comparable_types(op, left.type, right.type): raise TypeError(
            'Incomparable types: %r and %r' % (left.type, right.type))
        if op == '<>': op = '!='
        if left.type is NoneType:
            assert right.type is not NoneType
            left, right = right, left
        if right.type is NoneType:
            if op == '==': op = 'is'
            elif op == '!=': op = 'is not'
        elif op == 'is': op = '=='
        elif op == 'is not': op = '!='
        BoolMonad.__init__(monad, left.translator)
        monad.op = op
        monad.left = left
        monad.right = right
    def negate(monad):
        return CmpMonad(cmp_negate[monad.op], monad.left, monad.right)
    def getsql(monad):
        op = monad.op
        sql = []
        left_sql = monad.left.getsql()
        if op == 'is':
            return sqland([ [ IS_NULL, item ] for item in left_sql ])
        if op == 'is not':
            return sqland([ [ IS_NOT_NULL, item ] for item in left_sql ])
        right_sql = monad.right.getsql()
        assert len(left_sql) == len(right_sql)
        if op in ('<', '<=', '>', '>='):
            assert len(left_sql) == len(right_sql) == 1
            return [ cmp_ops[op], left_sql[0], right_sql[0] ]
        if op == '==':
            return sqland([ [ EQ, a, b ] for (a, b) in zip(left_sql, right_sql) ])
        if op == '!=':
            return sqlor([ [ NE, a, b ] for (a, b) in zip(left_sql, right_sql) ])
        assert False

class LogicalBinOpMonad(BoolMonad):
    def __init__(monad, operands):
        assert len(operands) >= 2
        operands = list(operands)
        for i, operand in enumerate(operands):
            if operand.type is not bool: operands[i] = operand.nonzero()
        BoolMonad.__init__(monad, operands[0].translator)
        monad.operands = operands
    def getsql(monad):
        return [ monad.binop ] + [ operand.getsql() for operand in monad.operands ]

class AndMonad(LogicalBinOpMonad):
    binop = AND

class OrMonad(LogicalBinOpMonad):
    binop = OR

class NotMonad(BoolMonad):
    def __init__(monad, operand):
        if operand.type is not bool: operand = operand.nonzero()
        BoolMonad.__init__(monad, operand.translator)
        monad.operand = operand
    def negate(monad):
        return monad.operand
    def getsql(monad):
        return [ NOT, monad.operand.getsql() ]

class FuncMonad(Monad):
    type = None
    def __init__(monad, translator):
        monad.translator = translator

special_functions = {}

def func_monad(func, type):
    def decorator(monad_method):
        class SpecificFuncMonad(FuncMonad):
            def __call__(monad, *args, **keyargs):
                for arg in args:
                    assert isinstance(arg, Monad)
                for value in keyargs.values():
                    assert isinstance(value, Monad)
                return monad_method(monad, *args, **keyargs)
        SpecificFuncMonad.type = type
        SpecificFuncMonad.__name__ = monad_method.__name__
        assert func not in special_functions
        special_functions[func] = SpecificFuncMonad
        return SpecificFuncMonad
    return decorator

@func_monad(Decimal, type=Decimal)
def FuncDecimalMonad(monad, x):
    if not isinstance(x, StringConstMonad): raise TypeError
    return ConstMonad(monad.translator, Decimal(x.value))

@func_monad(date, type=date)
def FuncDateMonad(monad, year, month, day):
    for x, name in zip((year, month, day), ('year', 'month', 'day')):
        if not isinstance(x, NumericMixin) or x.type is not int: raise TypeError(
            "'%s' argument of date(year, month, day) function must be int" % name)
        if not isinstance(x, ConstMonad): raise NotImplementedError
    return ConstMonad(monad.translator, date(year.value, month.value, day.value))

@func_monad(datetime, type=datetime)
def FuncDatetimeMonad(monad, *args):
    for x, name in zip(args, ('year', 'month', 'day', 'hour', 'minute', 'second', 'microsecond')):
        if not isinstance(x, NumericMixin) or x.type is not int: raise TypeError(
            "'%s' argument of datetime(...) function must be int" % name)
        if not isinstance(x, ConstMonad): raise NotImplementedError
    return ConstMonad(monad.translator, datetime(*tuple(arg.value for arg in args)))

@func_monad(len, type=int)
def FuncLenMonad(monad, x):
    return x.len()

@func_monad(abs, type=int)
def FuncAbsMonad(monad, x):
    return x.abs()

@func_monad(sum, type=int)
def FuncSumMonad(monad, x):
    return x.sum()

@func_monad(min, type=None)
def FuncMinMonad(monad, *args):
    if not args: raise TypeError
    if len(args) == 1: return args[0].min()
    return minmax(monad, MIN, *args)

@func_monad(max, type=None)
def FuncMaxMonad(monad, *args):
    if not args: raise TypeError
    if len(args) == 1: return args[0].max()
    return minmax(monad, MAX, *args)

def minmax(monad, sqlop, *args):
    assert len(args) > 1
    sql = [ sqlop ] + [ arg.getsql()[0] for arg in args ]
    arg_types = set(arg.type for arg in args)
    if len(arg_types) > 1: raise TypeError
    result_type = arg_types.pop()
    if result_type not in comparable_types: raise TypeError
    return ExprMonad(monad.translator, result_type, sql)

@func_monad(select, type=None)
def FuncSelectMonad(monad, subquery):
    if not isinstance(subquery, QuerySetMonad): raise TypeError
    return subquery

@func_monad(exists, type=None)
def FuncExistsMonad(monad, subquery):
    if not isinstance(subquery, SetMixin): raise TypeError
    return subquery.nonzero()

class SetMixin(object):
    pass

class AttrSetMonad(SetMixin, Monad):
    def __init__(monad, root, path):
        if root.translator.inside_expr: raise NotImplementedError
        item_type = normalize_type(path[-1].py_type)
        Monad.__init__(monad, root.translator, (item_type,))
        monad.root = root
        monad.path = path
    def cmp(monad, op, monad2):
        raise NotImplementedError
    def contains(monad, item, not_in=False):
        item_type = monad.type[0]
        if not are_comparable_types('==', item_type, item.type): raise TypeError, [item_type, item.type ]
        if isinstance(item_type, EntityMeta) and len(item_type._pk_columns_) > 1:
            raise NotImplementedError

        alias, expr, from_ast, conditions = monad._subselect()
        if expr is None:
            assert isinstance(item_type, EntityMeta)
            expr = [ COLUMN, alias, item_type._pk_columns_[0] ]
        subquery_ast = [ SELECT, [ ALL, expr ], from_ast, [ WHERE, sqland(conditions) ] ]
        sqlop = not_in and NOT_IN or IN
        return BoolExprMonad(monad.translator, [ sqlop, item.getsql()[0], subquery_ast ])
    def getattr(monad, name):
        item_type = monad.type[0]
        if not isinstance(item_type, EntityMeta):
            raise AttributeError, name
        entity = item_type
        attr = entity._adict_.get(name)
        if attr is None: raise AttributeError, name
        return AttrSetMonad(monad.root, monad.path + [ attr ])
    def len(monad):
        if not monad.path[-1].reverse: kind = DISTINCT
        else: kind = ALL
        alias, expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [ COUNT, kind, expr ] ], from_ast, [ WHERE, sqland(conditions) ] ]
        return NumericExprMonad(monad.translator, int, sql_ast)
    def sum(monad):
        item_type = monad.type[0]
        if item_type not in numeric_types: raise TypeError
        alias, expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [COALESCE, [ SUM, expr ], [ VALUE, 0 ]]], from_ast, [ WHERE, sqland(conditions) ] ]
        return NumericExprMonad(monad.translator, item_type, sql_ast)
    def min(monad):
        item_type = monad.type[0]
        if item_type not in comparable_types: raise TypeError
        alias, expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [ MIN, expr ] ], from_ast, [ WHERE, sqland(conditions) ] ]
        return ExprMonad.new(monad.translator, item_type, sql_ast)
    def max(monad):
        item_type = monad.type[0]
        if item_type not in comparable_types: raise TypeError
        alias, expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [ MAX, expr ] ], from_ast, [ WHERE, sqland(conditions) ] ]
        return ExprMonad.new(monad.translator, item_type, sql_ast)
    def nonzero(monad):
        alias, expr, from_ast, conditions = monad._subselect()
        sql_ast = [ EXISTS, from_ast, [ WHERE, sqland(conditions) ] ]
        return BoolExprMonad(monad.translator, sql_ast)
    def negate(monad):
        alias, expr, from_ast, conditions = monad._subselect()
        sql_ast = [ NOT_EXISTS, from_ast, [ WHERE, sqland(conditions) ] ]
        return BoolExprMonad(monad.translator, sql_ast)
    def _subselect(monad):
        from_ast = [ FROM ]
        conditions = []
        alias = None
        prev_alias = monad.root.alias
        expr = None 
        for attr in monad.path:
            prev_entity = attr.entity
            reverse = attr.reverse
            if not reverse:
                assert attr is monad.path[-1] and len(attr.columns) == 1
                expr = [ COLUMN, alias, attr.column ]
                if not attr.is_required:
                    conditions.append([ IS_NOT_NULL, [ COLUMN, alias, attr.column ] ])
                break
            
            next_entity = attr.py_type
            assert isinstance(next_entity, EntityMeta)
            alias = '-'.join((prev_alias, attr.name))
            alias = monad.translator.get_short_alias(alias, next_entity.__name__)
            if not attr.is_collection:
                from_ast.append([ alias, TABLE, next_entity._table_ ])
                if attr.columns:                    
                    join_tables(conditions, prev_alias, alias, attr.columns, next_entity._pk_columns_)
                else:
                    assert not reverse.is_collection and reverse.columns
                    join_tables(conditions, prev_alias, alias, prev_entity._pk_columns_, reverse.columns)
            elif reverse.is_collection:
                m2m_table = attr.table
                m2m_alias = monad.translator.get_short_alias(None, 'm2m-')
                from_ast.append([ m2m_alias, TABLE, m2m_table ])
                join_tables(conditions, prev_alias, m2m_alias, prev_entity._pk_columns_, reverse.columns)
                from_ast.append([ alias, TABLE, next_entity._table_ ])
                join_tables(conditions, m2m_alias, alias, attr.columns, next_entity._pk_columns_)
            else:
                from_ast.append([ alias, TABLE, next_entity._table_ ])
                join_tables(conditions, prev_alias, alias, prev_entity._pk_columns_, reverse.columns)
            prev_alias = alias
        assert alias is not None
        return alias, expr, from_ast, conditions
    def getsql(monad):
        raise TranslationError

class QuerySetMonad(SetMixin, Monad):
    def __init__(monad, translator, subtranslator):        
        monad.subtranslator = subtranslator
        attr, attr_type = monad._get_attr_info()
        item_type = attr_type or subtranslator.entity
        monad.item_type = item_type
        monad_type = (item_type,)  # todo: better way to represent type "Set of item_type"
        Monad.__init__(monad, translator, monad_type)
    def _get_attr_info(monad):
        sub = monad.subtranslator
        if sub.attrname is None: return None, None
        attr = sub.entity._adict_[sub.attrname]
        return attr, normalize_type(attr.py_type)
    def contains(monad, item, not_in=False):
        item_type = monad.type[0]
        if not are_comparable_types('==', item_type, item.type): raise TypeError, [item_type, item.type ]
        if isinstance(item_type, EntityMeta) and len(item_type._pk_columns_) > 1:
            raise NotImplementedError

        attr, attr_type = monad._get_attr_info()
        if attr is None: columns = item_type._pk_columns_
        else: columns = attr.columns
        if len(columns) > 1: raise NotImplementedError

        sub = monad.subtranslator
        select_ast = [ ALL, [ COLUMN, sub.alias, columns[0] ] ]
        conditions = sub.conditions[:]
        if attr is not None and not attr.is_required:
            conditions.append([ IS_NOT_NULL, [ COLUMN, sub.alias, columns[0] ]])
        subquery_ast = [ SELECT, select_ast, sub.from_, [ WHERE, sqland(conditions) ] ]
        sqlop = not_in and NOT_IN or IN
        return BoolExprMonad(monad.translator, [ sqlop, item.getsql()[0], subquery_ast ])
    def nonzero(monad):        
        sub = monad.subtranslator
        sql_ast = [ EXISTS, sub.from_, [ WHERE, sqland(sub.conditions) ] ]
        return BoolExprMonad(monad.translator, sql_ast)
    def negate(monad):
        sub = monad.subtranslator
        sql_ast = [ NOT_EXISTS, sub.from_, [ WHERE, sqland(sub.conditions) ] ]
        return BoolExprMonad(monad.translator, sql_ast)
    def _subselect(monad, item_type, select_ast):
        sub = monad.subtranslator
        sql_ast = [ SELECT, select_ast, sub.from_, [ WHERE, sqland(sub.conditions) ] ]
        return ExprMonad.new(monad.translator, item_type, sql_ast)
    def len(monad):
        attr, attr_type = monad._get_attr_info()
        if attr is not None:
            if len(attr.columns) > 1: raise NotImplementedError
            select_ast = [ AGGREGATES, [ COUNT, DISTINCT, [ COLUMN, monad.subtranslator.alias, attr.column ] ] ]
        else: select_ast = [ AGGREGATES, [ COUNT, ALL ] ]
        return monad._subselect(int, select_ast)
    def sum(monad):
        attr, attr_type = monad._get_attr_info()
        if attr_type not in numeric_types: raise TypeError
        select_ast = [ AGGREGATES, [ COALESCE, [ SUM, [ COLUMN, monad.subtranslator.alias, attr.column ] ], [ VALUE, 0 ] ] ]
        return monad._subselect(attr_type, select_ast)
    def min(monad):
        attr, attr_type = monad._get_attr_info()
        if attr_type not in comparable_types: raise TypeError
        select_ast = [ AGGREGATES, [ MIN, [ COLUMN, monad.subtranslator.alias, attr.column ] ] ]
        return monad._subselect(attr_type, select_ast)
    def max(monad):
        attr, attr_type = monad._get_attr_info()
        if attr_type not in comparable_types: raise TypeError
        select_ast = [ AGGREGATES, [ MAX, [ COLUMN, monad.subtranslator.alias, attr.column ] ] ]
        return monad._subselect(attr_type, select_ast)
