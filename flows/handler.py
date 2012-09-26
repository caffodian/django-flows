from django.conf.urls import patterns, url, include
from django.core.exceptions import ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from flows import config
from flows.components import Scaffold, Action, name_for_flow, COMPLETE, \
    get_by_class_or_name
from flows.history import FlowHistory
from flows.statestore import state_store
from flows.statestore.base import StateNotFound
import re
import uuid
import inspect



class FlowHandler(object):
    
    def __init__(self):
        self._entry_points = []
    
    def _get_state(self, task_id, create=False):
        
        if not re.match('^[0-9a-f]{32}$', task_id):
            # someone is messing with the task ID - don't even try
            # to do anything with it
            raise StateNotFound
        
        try:
            return state_store.get_state(task_id)
        except StateNotFound:
            if not create:
                raise
            
        # create a task and some state
        task_id = re.sub('-', '', str(uuid.uuid4()))
        state = {'_id': task_id }
        state_store.put_state(task_id, state)
        
        return state

    
    def _view(self, position):

        def handle_view(request, *args, **kwargs):
            # first get the state for this task, or create state if
            # this is an entry point with no state
            if config.FLOWS_TASK_ID_PARAM in request.REQUEST:
                task_id = request.REQUEST[config.FLOWS_TASK_ID_PARAM]
                create = False
            else:
                task_id = re.sub('-', '', str(uuid.uuid4()))
                create = True
            state = self._get_state(task_id, create=create)

            # create the instances required to handle the request 
            flow_instance = position.create_instance(state)
                
            # deal with the request
            return flow_instance.handle(request, *args, **kwargs)

        return handle_view

    
    def _urls_for_flow(self, flow_component, flow_position=None):

        urlpatterns = []
        
        if flow_position is None:
            flow_position = PossibleFlowPosition([flow_component])
        else:
            flow_position = PossibleFlowPosition(flow_position.flow_component_classes + [flow_component])
        
        if hasattr(flow_component, 'urls'):
            flow_urls = flow_component.urls
        else:
            flow_urls = [flow_component.url]

        if issubclass(flow_component, Scaffold) and hasattr(flow_component, 'action_set'):
            for child in flow_component.action_set:
                for u in flow_urls:
                    urlpatterns += patterns('', url(u, include(self._urls_for_flow(child, flow_position))))

        elif issubclass(flow_component, Action):
            name = flow_position.url_name
            for u in flow_urls:
                urlpatterns += patterns('', url(u, self._view(flow_position), name=name))

        else:
            raise TypeError(str(flow_component))

        return urlpatterns
    
    
    def register_entry_point(self, flow_component):
        self._entry_points.append( flow_component )
        
    @property
    def urls(self):
        urlpatterns = []
        for flow in self._entry_points:
            urlpatterns += self._urls_for_flow(flow)
        return urlpatterns


class FlowPositionInstance(object):
    """
    A FlowPositionInstance represents a concrete instance of a PossibleFlowPosition - 
    that is, a user is currently performing an action as part of a flow
    """
    
    def __init__(self, position, state):
        self._position = position
        self._state = state
        self._flow_components = []
        
        for flow_component_class in self._position.flow_component_classes:
            flow_component = flow_component_class()
            flow_component._flow_position_instance = self
            flow_component.state = state
            
            self._flow_components.append( flow_component )
            
        self._history = FlowHistory(self)    
            
        self._validate()
        
    def _validate(self):
        pass
        # TODO: assert that only the last element is an Action and that the
        # rest are Scaffolds
        
    @property
    def task_id(self):
        return self._state['_id']
            
    def get_root_component(self):
        return self._flow_components[0]
    
    def get_action(self):
        return self._flow_components[-1]
    
    def get_back_url(self):
        return self._history.get_back_url()
    
    def get_absolute_url(self):
        args=[]
        kwargs={}
        for flow_component in self._flow_components:
            flow_args, flow_kwargs = flow_component.get_url_args()
            args += flow_args
            kwargs.update(flow_kwargs)
            
        url_name = self._position.url_name
        url = reverse(url_name, args=args, kwargs=kwargs)
        
        separator = '&' if '?' in url else '?'
        
        return '%(url)s%(separator)s%(task_id_param_name)s=%(task_id)s' % { 
                                 'url': url, 'separator': separator,
                                 'task_id_param_name': config.FLOWS_TASK_ID_PARAM,
                                 'task_id': self.task_id  }

    def position_instance_for(self, component_class_or_name):
        # figure out where we're being sent to
        FC = get_by_class_or_name(component_class_or_name)
        
        # it should be a sibling of one of the current items
        # for example, if we are in position [A,B,E]:
        #
        #         A
        #      /  |  \
        #    B    C   D
        #   /  \      |  \
        #  E   F      G   H
        # 
        #  E can send to F (its own sibling) or C (sibling of its parent)

        fci = None
        for fci in self._flow_components[-2::-1]: # go backwards but skip the last element (the action)
            if FC in fci.action_set:
                # we found the relevant action set, which means we know the root
                # part of the tree, and now we can construct the rest
                break
        
        idx = self._flow_components.index(fci)
        
        # so the new tree is from the root to the parent of the one we just found,
        # coupled with the initial subtree from the component we're tring to redirect
        # to
        tree_root = self._position.flow_component_classes[:idx+1]
        
        # figure out the action tree for the new first component - either
        # we have been given an action, in which case it's just one single
        # item, or we have been given a scaffold, in which case there could
        # be a list of [scaffold, scaffold..., action]
        new_subtree = FC.get_initial_action_tree()
        
        # we use our current tree and replace the current leaf with this new 
        # subtree to get the new position
        new_position = PossibleFlowPosition(tree_root + new_subtree)
        
        # now create an instance of the position with the current state
        return new_position.create_instance(self._state)

    
    def handle(self, request, *args, **kwargs):
        # first validate that we can actually run by checking for
        # required state, for example
        for flow_component in self._flow_components:
            flow_component.check_preconditions(request)
            
        # now call each of the prepare methods for the components
        response = None
        for flow_component in self._flow_components:
            response = flow_component.prepare(request, *args, **kwargs)
            if response is not None:
                # we allow prepare methods to give out responses if they
                # want to, eg, redirect
                break
                
        if response is None:
            # now that everything is set up, we can handle the request
            response = self.get_action().dispatch(request, *args, **kwargs)
            
            # if this is a GET request, then we displayed something to the user, so
            # we should record this in the history, unless the request returned a 
            # redirect, in which case we haven't displayed anything
            if request.method == 'GET' and not isinstance(response, HttpResponseRedirect):
                self._history.add_to_history(self)
        
        # now we have a response, we need to decide what to do with it
        for flow_component in self._flow_components[::-1]: # go from leaf to root, ie, backwards
            response = flow_component.handle_response(response)
            
        # now we have some kind of response, figure out what it is exactly
        if response == COMPLETE:
            # this means that the entire flow finished - we should redirect
            # to the on_complete url if we have one, or get upset if we don't
            next_url = self._state.get('_on_complete', None)
            if next_url is None:
                # oh, we don't know where to go...
                raise ImproperlyConfigured('Flow completed without an _on_complete URL or an explicit redirect - %s' % self.__repr__())
            else:
                response = redirect(next_url)

            # if we are done, then we should remove the task state
            state_store.delete(self.task_id)
            
        else:
            # update the state if necessary
            state_store.put_state(self.task_id, self._state)
            
            if inspect.isclass(response):
                # we got given a class, which implies the code should redirect
                # to this new (presumably Action) class
                response = redirect(self.position_instance_for(response).get_absolute_url()) 
            
            elif isinstance(response, Action):
                # this is a new action for the user, so redirect to it
                url = response.get_absolute_url()
                response = redirect(url)
                
            elif isinstance(response, basestring):
                # this is a string which should be the name of an action
                # which couldn't be referenced as a class for some reason
                flow_component = get_by_class_or_name(response)
                response = redirect(flow_component.get_absolute_url()) 

        return response
    
    def __repr__(self):
        return 'Instance of %s' % self._position.__repr__()
        
    
class PossibleFlowPosition(object):
    all_positions = {}
    
    """
    A PossibleFlowPosition represents a possible position in a hierachy of 
    flow components. On startup, all FlowComponents (Scaffolds and Actions)
    are inspected to build up a list of all possible positions within all
    avaiable flows. This class represents one such possibility.
    """

    def __init__(self, flow_components):
        self.flow_component_classes = flow_components
            
        PossibleFlowPosition.all_positions[self.url_name] = self
            
    def create_instance(self, state):
        return FlowPositionInstance(self, state)
    
    def position_for_new_subtree(self, action_sublist):
        components = self.flow_component_classes[:-1] + action_sublist
        new_url_name = self._url_name_from_components(components)
        return PossibleFlowPosition.all_positions[new_url_name]
    
    def _url_name_from_components(self, components):
        return 'flow_%s' % '/'.join([name_for_flow(fc) for fc in components])
    
    @property
    def url_name(self):
        return self._url_name_from_components(self.flow_component_classes)
    
    def __repr__(self):
        return ' / '.join( map(str, self.flow_component_classes) )
    
    
