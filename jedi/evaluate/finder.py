"""
Searching for names with given scope and name. This is very central in Jedi and
Python. The name resolution is quite complicated with descripter,
``__getattribute__``, ``__getattr__``, ``global``, etc.

If you want to understand name resolution, please read the first few chapters
in http://blog.ionelmc.ro/2015/02/09/understanding-python-metaclasses/.

Flow checks
+++++++++++

Flow checks are not really mature. There's only a check for ``isinstance``.  It
would check whether a flow has the form of ``if isinstance(a, type_or_tuple)``.
Unfortunately every other thing is being ignored (e.g. a == '' would be easy to
check for -> a is a string). There's big potential in these checks.
"""

from parso.python import tree
from parso.tree import search_ancestor
from jedi import debug
from jedi import settings
from jedi.evaluate import representation as er
from jedi.evaluate.instance import AbstractInstanceContext
from jedi.evaluate import compiled
from jedi.evaluate import pep0484
from jedi.evaluate import iterable
from jedi.evaluate import imports
from jedi.evaluate import analysis
from jedi.evaluate import flow_analysis
from jedi.evaluate import param
from jedi.evaluate import helpers
from jedi.evaluate.filters import get_global_filters, TreeNameDefinition
from jedi.evaluate.context import ContextualizedName, ContextualizedNode, ContextSet
from jedi.parser_utils import is_scope, get_parent_scope
from jedi.evaluate.syntax_tree import eval_trailer


class NameFinder(object):
    def __init__(self, evaluator, context, name_context, name_or_str,
                 position=None, analysis_errors=True):
        self._evaluator = evaluator
        # Make sure that it's not just a syntax tree node.
        self._context = context
        self._name_context = name_context
        self._name = name_or_str
        if isinstance(name_or_str, tree.Name):
            self._string_name = name_or_str.value
        else:
            self._string_name = name_or_str
        self._position = position
        self._found_predefined_types = None
        self._analysis_errors = analysis_errors

    @debug.increase_indent
    def find(self, filters, attribute_lookup):
        """
        :params bool attribute_lookup: Tell to logic if we're accessing the
            attribute or the contents of e.g. a function.
        """
        names = self.filter_name(filters)
        if self._found_predefined_types is not None and names:
            check = flow_analysis.reachability_check(
                self._context, self._context.tree_node, self._name)
            if check is flow_analysis.UNREACHABLE:
                return ContextSet()
            return self._found_predefined_types

        types = self._names_to_types(names, attribute_lookup)

        if not names and self._analysis_errors and not types \
                and not (isinstance(self._name, tree.Name) and
                         isinstance(self._name.parent.parent, tree.Param)):
            if isinstance(self._name, tree.Name):
                if attribute_lookup:
                    analysis.add_attribute_error(
                        self._name_context, self._context, self._name)
                else:
                    message = ("NameError: name '%s' is not defined."
                               % self._string_name)
                    analysis.add(self._name_context, 'name-error', self._name, message)

        return types

    def _get_origin_scope(self):
        if isinstance(self._name, tree.Name):
            scope = self._name
            while scope.parent is not None:
                # TODO why if classes?
                if not isinstance(scope, tree.Scope):
                    break
                scope = scope.parent
            return scope
        else:
            return None

    def get_filters(self, search_global=False):
        origin_scope = self._get_origin_scope()
        if search_global:
            return get_global_filters(self._evaluator, self._context, self._position, origin_scope)
        else:
            return self._context.get_filters(search_global, self._position, origin_scope=origin_scope)

    def filter_name(self, filters):
        """
        Searches names that are defined in a scope (the different
        ``filters``), until a name fits.
        """
        names = []
        if self._context.predefined_names:
            # TODO is this ok? node might not always be a tree.Name
            node = self._name
            while node is not None and not is_scope(node):
                node = node.parent
                if node.type in ("if_stmt", "for_stmt", "comp_for"):
                    try:
                        name_dict = self._context.predefined_names[node]
                        types = name_dict[self._string_name]
                    except KeyError:
                        continue
                    else:
                        self._found_predefined_types = types
                        break

        for filter in filters:
            names = filter.get(self._string_name)
            if names:
                if len(names) == 1:
                    n, = names
                    if isinstance(n, TreeNameDefinition):
                        # Something somewhere went terribly wrong. This
                        # typically happens when using goto on an import in an
                        # __init__ file. I think we need a better solution, but
                        # it's kind of hard, because for Jedi it's not clear
                        # that that name has not been defined, yet.
                        if n.tree_name == self._name:
                            if self._name.get_definition().type == 'import_from':
                                continue
                break

        debug.dbg('finder.filter_name "%s" in (%s): %s@%s', self._string_name,
                  self._context, names, self._position)
        return list(names)

    def _check_getattr(self, inst):
        """Checks for both __getattr__ and __getattribute__ methods"""
        # str is important, because it shouldn't be `Name`!
        name = compiled.create(self._evaluator, self._string_name)

        # This is a little bit special. `__getattribute__` is in Python
        # executed before `__getattr__`. But: I know no use case, where
        # this could be practical and where Jedi would return wrong types.
        # If you ever find something, let me know!
        # We are inversing this, because a hand-crafted `__getattribute__`
        # could still call another hand-crafted `__getattr__`, but not the
        # other way around.
        names = (inst.get_function_slot_names('__getattr__') or
                 inst.get_function_slot_names('__getattribute__'))
        return inst.execute_function_slots(names, name)

    def _names_to_types(self, names, attribute_lookup):
        contexts = ContextSet.from_sets(name.infer() for name in names)

        debug.dbg('finder._names_to_types: %s -> %s', names, contexts)
        if not names and isinstance(self._context, AbstractInstanceContext):
            # handling __getattr__ / __getattribute__
            return self._check_getattr(self._context)

        # Add isinstance and other if/assert knowledge.
        if not contexts and isinstance(self._name, tree.Name) and \
                not isinstance(self._name_context, AbstractInstanceContext):
            flow_scope = self._name
            base_node = self._name_context.tree_node
            if base_node.type == 'comp_for':
                return contexts
            while True:
                flow_scope = get_parent_scope(flow_scope, include_flows=True)
                n = _check_flow_information(self._name_context, flow_scope,
                                            self._name, self._position)
                if n is not None:
                    return n
                if flow_scope == base_node:
                    break
        return contexts


def _name_to_types(evaluator, context, tree_name):
    types = []
    node = tree_name.get_definition(import_name_always=True)
    if node is None:
        node = tree_name.parent
        if node.type == 'global_stmt':
            context = evaluator.create_context(context, tree_name)
            finder = NameFinder(evaluator, context, context, tree_name.value)
            filters = finder.get_filters(search_global=True)
            # For global_stmt lookups, we only need the first possible scope,
            # which means the function itself.
            filters = [next(filters)]
            return finder.find(filters, attribute_lookup=False)
        elif node.type not in ('import_from', 'import_name'):
            raise ValueError("Should not happen.")

    typ = node.type
    if typ == 'for_stmt':
        types = pep0484.find_type_from_comment_hint_for(context, node, tree_name)
        if types:
            return types
    if typ == 'with_stmt':
        types = pep0484.find_type_from_comment_hint_with(context, node, tree_name)
        if types:
            return types

    if typ in ('for_stmt', 'comp_for'):
        try:
            types = context.predefined_names[node][tree_name.value]
        except KeyError:
            cn = ContextualizedNode(context, node.children[3])
            for_types = iterable.py__iter__types(evaluator, cn.infer(), cn)
            c_node = ContextualizedName(context, tree_name)
            types = check_tuple_assignments(evaluator, c_node, for_types)
    elif typ == 'expr_stmt':
        types = _remove_statements(evaluator, context, node, tree_name)
    elif typ == 'with_stmt':
        context_managers = context.eval_node(node.get_test_node_from_name(tree_name))
        enter_methods = context_managers.py__getattribute__('__enter__')
        return enter_methods.execute_evaluated()
    elif typ in ('import_from', 'import_name'):
        types = imports.infer_import(context, tree_name)
    elif typ in ('funcdef', 'classdef'):
        types = _apply_decorators(evaluator, context, node)
    elif typ == 'try_stmt':
        # TODO an exception can also be a tuple. Check for those.
        # TODO check for types that are not classes and add it to
        # the static analysis report.
        exceptions = context.eval_node(tree_name.get_previous_sibling().get_previous_sibling())
        types = exceptions.execute_evaluated()
    else:
        raise ValueError("Should not happen.")
    return types


def _apply_decorators(evaluator, context, node):
    """
    Returns the function, that should to be executed in the end.
    This is also the places where the decorators are processed.
    """
    if node.type == 'classdef':
        decoratee_context = er.ClassContext(
            evaluator,
            parent_context=context,
            classdef=node
        )
    else:
        decoratee_context = er.FunctionContext(
            evaluator,
            parent_context=context,
            funcdef=node
        )
    initial = values = ContextSet(decoratee_context)
    for dec in reversed(node.get_decorators()):
        debug.dbg('decorator: %s %s', dec, values)
        dec_values = context.eval_node(dec.children[1])
        trailer_nodes = dec.children[2:-1]
        if trailer_nodes:
            # Create a trailer and evaluate it.
            trailer = tree.PythonNode('trailer', trailer_nodes)
            trailer.parent = dec
            dec_values = eval_trailer(context, dec_values, trailer)

        if not len(dec_values):
            debug.warning('decorator not found: %s on %s', dec, node)
            return initial

        values = dec_values.execute(param.ValuesArguments([values]))
        if not len(values):
            debug.warning('not possible to resolve wrappers found %s', node)
            return initial

        debug.dbg('decorator end %s', values)
    return values


def _remove_statements(evaluator, context, stmt, name):
    """
    This is the part where statements are being stripped.

    Due to lazy evaluation, statements like a = func; b = a; b() have to be
    evaluated.
    """
    pep0484_contexts = \
        pep0484.find_type_from_comment_hint_assign(context, stmt, name)
    if pep0484_contexts:
        return pep0484_contexts

    return context.eval_stmt(stmt, seek_name=name)


def _check_flow_information(context, flow, search_name, pos):
    """ Try to find out the type of a variable just with the information that
    is given by the flows: e.g. It is also responsible for assert checks.::

        if isinstance(k, str):
            k.  # <- completion here

    ensures that `k` is a string.
    """
    if not settings.dynamic_flow_information:
        return None

    result = None
    if is_scope(flow):
        # Check for asserts.
        module_node = flow.get_root_node()
        try:
            names = module_node.get_used_names()[search_name.value]
        except KeyError:
            return None
        names = reversed([
            n for n in names
            if flow.start_pos <= n.start_pos < (pos or flow.end_pos)
        ])

        for name in names:
            ass = search_ancestor(name, 'assert_stmt')
            if ass is not None:
                result = _check_isinstance_type(context, ass.assertion, search_name)
                if result is not None:
                    return result

    if flow.type in ('if_stmt', 'while_stmt'):
        potential_ifs = [c for c in flow.children[1::4] if c != ':']
        for if_test in reversed(potential_ifs):
            if search_name.start_pos > if_test.end_pos:
                return _check_isinstance_type(context, if_test, search_name)
    return result


def _check_isinstance_type(context, element, search_name):
    try:
        assert element.type in ('power', 'atom_expr')
        # this might be removed if we analyze and, etc
        assert len(element.children) == 2
        first, trailer = element.children
        assert first.type == 'name' and first.value == 'isinstance'
        assert trailer.type == 'trailer' and trailer.children[0] == '('
        assert len(trailer.children) == 3

        # arglist stuff
        arglist = trailer.children[1]
        args = param.TreeArguments(context.evaluator, context, arglist, trailer)
        param_list = list(args.unpack())
        # Disallow keyword arguments
        assert len(param_list) == 2
        (key1, lazy_context_object), (key2, lazy_context_cls) = param_list
        assert key1 is None and key2 is None
        call = helpers.call_of_leaf(search_name)
        is_instance_call = helpers.call_of_leaf(lazy_context_object.data)
        # Do a simple get_code comparison. They should just have the same code,
        # and everything will be all right.
        normalize = context.evaluator.grammar._normalize
        assert normalize(is_instance_call) == normalize(call)
    except AssertionError:
        return None

    context_set = ContextSet()
    for cls_or_tup in lazy_context_cls.infer():
        if isinstance(cls_or_tup, iterable.AbstractSequence) and \
                cls_or_tup.array_type == 'tuple':
            for lazy_context in cls_or_tup.py__iter__():
                for context in lazy_context.infer():
                    context_set |= context.execute_evaluated()
        else:
            context_set |= cls_or_tup.execute_evaluated()
    return context_set


def check_tuple_assignments(evaluator, contextualized_name, context_set):
    """
    Checks if tuples are assigned.
    """
    lazy_context = None
    for index, node in contextualized_name.assignment_indexes():
        cn = ContextualizedNode(contextualized_name.context, node)
        iterated = iterable.py__iter__(evaluator, context_set, cn)
        for _ in range(index + 1):
            try:
                lazy_context = next(iterated)
            except StopIteration:
                # We could do this with the default param in next. But this
                # would allow this loop to run for a very long time if the
                # index number is high. Therefore break if the loop is
                # finished.
                return ContextSet()
        context_set = lazy_context.infer()
    return context_set
