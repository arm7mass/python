# -*- coding: utf-8 -*-
#!/usr/bin/env python
#
# Copyright 2015 BigML
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A local Predictive Logistic Regression.

This module defines a Logistic Regression to make predictions locally or
embedded into your application without needing to send requests to
BigML.io.

This module cannot only save you a few credits, but also enormously
reduce the latency for each prediction and let you use your clusters
offline.

Example usage (assuming that you have previously set up the BIGML_USERNAME
and BIGML_API_KEY environment variables and that you own the
logisticregression/id below):

from bigml.api import BigML
from bigml.logistic import LogisticRegression

api = BigML()

logistic_regression = LogisticRegression(
    'logisticregression/5026965515526876630001b2')
logistic_regression.predict({"petal length": 3, "petal width": 1,
                             "sepal length": 1, "sepal width": 0.5})

"""
import logging
LOGGER = logging.getLogger('BigML')

import sys
import math
import re

from bigml.api import FINISHED
from bigml.api import (BigML, get_logistic_regression_id, get_status)
from bigml.util import cast, utf8
from bigml.basemodel import retrieve_resource, extract_objective
from bigml.basemodel import ONLY_MODEL
from bigml.model import print_distribution
from bigml.model import STORAGE
from bigml.predicate import TM_TOKENS, TM_FULL_TERM
from bigml.modelfields import ModelFields
from bigml.io import UnicodeWriter

EXPANSION_ATTRIBUTES = {"categorical": "categories", "text": "tag_cloud"}
OPTIONAL_FIELDS = ['categorical', 'text']

def parse_terms(text, case_sensitive=True):
    """Returns the list of parsed terms

    """
    if text is None:
        return []
    expression = ur'(\b|_)([^\b_\s]+?)(\b|_)'
    pattern = re.compile(expression)
    return map(lambda x: x[1] if case_sensitive else x[1].lower(),
               re.findall(pattern, text))


def get_unique_terms(terms, term_forms, tag_cloud):
    """Extracts the unique terms that occur in one of the alternative forms in
       term_forms or in the tag cloud.

    """
    extend_forms = {}
    for term, forms in term_forms.items():
        for form in forms:
            extend_forms[form] = term
        extend_forms[term] = term
    terms_set = {}
    for term in terms:
        if term in tag_cloud:
            if not term in terms_set:
                terms_set[term] = 0
            terms_set[term] += 1
        elif term in extend_forms:
            term = extend_forms[term]
            if not term in terms_set:
                terms_set[term] = 0
            terms_set[term] += 1
    return terms_set.items()


class LogisticRegression(ModelFields):
    """ A lightweight wrapper around a logistic regression model.

    Uses a BigML remote logistic regression model to build a local version
    that can be used to generate predictions locally.

    """

    def __init__(self, logistic_regression, api=None):

        self.resource_id = None
        self.term_forms = {}
        self.tag_clouds = {}
        self.term_analysis = {}
        self.categories = {}
        self.coefficients = {}
        self.data_field_types = {}
        self.bias = None
        if not (isinstance(logistic_regression, dict)
                and 'resource' in logistic_regression and
                logistic_regression['resource'] is not None):
            if api is None:
                api = BigML(storage=STORAGE)
            self.resource_id = get_logistic_regression_id(logistic_regression)
            if self.resource_id is None:
                raise Exception(
                    api.error_message(logistic_regression,
                                      resource_type='logistic_regression',
                                      method='get'))
            query_string = ONLY_MODEL
            logistic_regression = retrieve_resource(
                api, self.resource_id, query_string=query_string)
        else:
            self.resource_id = get_logistic_regression_id(logistic_regression)

        if 'object' in logistic_regression and \
            isinstance(logistic_regression['object'], dict):
            logistic_regression = logistic_regression['object']
        try:
            self.dataset_field_types = logistic_regression.get(
                "dataset_field_types", {})
            objective_field = logistic_regression['objective_fields']
        except KeyError:
            raise ValueError("Failed to find the logistic regression expected "
                             "JSON structure. Check your arguments.")
        if 'logistic_regression' in logistic_regression and \
            isinstance(logistic_regression['logistic_regression'], dict):
            status = get_status(logistic_regression)
            if 'code' in status and status['code'] == FINISHED:
                logistic_regression_info = logistic_regression[ \
                    'logistic_regression']
                self.term_forms = {}
                self.tag_clouds = {}
                self.term_analysis = {}
                fields = logistic_regression_info.get('fields', {})

                self.coefficients.update(logistic_regression_info.get( \
                    'coefficients', []))
                self.bias = logistic_regression_info.get('bias', 0)
                for field_id, field in fields.items():
                    if field['optype'] == 'text':
                        self.term_forms[field_id] = {}
                        self.term_forms[field_id].update(
                            field['summary']['term_forms'])
                        self.tag_clouds[field_id] = {}
                        self.tag_clouds[field_id] = [tag for [tag, _] in field[
                            'summary']['tag_cloud']]
                        self.term_analysis[field_id] = {}
                        self.term_analysis[field_id].update(
                            field['term_analysis'])
                    if field['optype'] == 'categorical':
                        self.categories[field_id] = [category for [category, _]
                            in field['summary']['categories']]
                ModelFields.__init__(
                    self, fields,
                    objective_id=extract_objective(objective_field))
                self.map_coefficients()
                if len(self.fields) < self.dataset_field_types.get( \
                        "total", float("inf")):
                    print len(self.fields), self.dataset_field_types.get( \
                        "total", float("inf"))
                    raise Exception("Some fields are missing"
                                    " to generate a local logistic regression."
                                    " Please, provide a logistic regression"
                                    " with the complete list of fields.")
            else:
                raise Exception("The logistic regression isn't finished yet")
        else:
            raise Exception("Cannot create the LogisticRegression instance."
                            " Could not find the 'logistic_regression' key"
                            " in the resource:\n\n%s" %
                            logistic_regression)

    def predict(self, input_data, by_name=True):
        """Returns the class prediction and the probability distribution

        """
        # Checks and cleans input_data leaving the fields used in the model
        input_data = self.filter_input_data(input_data, by_name=by_name)

        # Checks that all numeric fields are present in input data
        for field_id, field in self.fields.items():
            if (not field['optype'] in OPTIONAL_FIELDS and
                    not field_id in input_data):
                raise Exception("Failed to predict. Input"
                                " data must contain values for all numeric "
                                "fields to get a logistic regression"
                                " prediction.")

        # Strips affixes for numeric values and casts to the final field type
        cast(input_data, self.fields)

        # Compute text and categorical field expansion
        unique_terms = self.get_unique_terms(input_data)

        probabilities = {}

        if len(self.categories[self.objective_id]) == 2:
            # binary classifications have only one set of coefficients and
            # you can compute the complementary probability as 1-p
            category = self.coefficients.keys()[0]
            coefficients = self.coefficients[category]
            probabilities[category] = self.category_probability(
                input_data, unique_terms, coefficients)
            category_2 = [category_2 for category_2 in
                          self.categories[self.objective_id]
                          if category_2 != category][0]
            probabilities[category_2] = 1 - probabilities[category]
        else:
            total = 0
            for category in self.categories[self.objective_id]:
                coefficients = self.coefficients[category]
                probabilities[category] = self.category_probability(
                    input_data, unique_terms, coefficients)
                total += probabilities[category]
            for category in probabilities.keys():
                probabilities[category] /= total
        predictions = sorted(probabilities.items(),
                             key=lambda x: x[1], reverse=True)
        prediction, probability = predictions[0]
        return {
            "prediction": prediction,
            "probability": probability,
            "distribution": [{"category": category, "probability": probability}
                             for category, probability in predictions]}

    def category_probability(self, input_data, unique_terms, coefficients):
        """Computes the probability for a concrete category

        """
        probability = 0

        for field_id in input_data:
            shift = self.fields[field_id]['coefficients_shift']
            probability += coefficients[shift] * input_data[field_id]

        for field_id in unique_terms:

            shift = self.fields[field_id]['coefficients_shift']
            for term, occurrences in unique_terms[field_id]:
                try:
                    if field_id in self.tag_clouds:
                        index = self.tag_clouds[field_id].index(term)
                        print index
                    elif field_id in self.categories:
                        index = self.categories[field_id].index(term)
                    probability += coefficients[shift + index] * occurrences
                except ValueError:
                    pass
        if self.bias > 0:
            probability += coefficients[-1]
        probability = 1 / (1 + math.exp(-probability))
        return probability

    def get_unique_terms(self, input_data):
        """Parses the input data to find the list of unique terms in the
           tag cloud

        """
        unique_terms = {}
        for field_id in self.term_forms:
            if field_id in input_data:
                input_data_field = input_data.get(field_id, '')
                if isinstance(input_data_field, basestring):
                    case_sensitive = self.term_analysis[field_id].get(
                        'case_sensitive', True)
                    token_mode = self.term_analysis[field_id].get(
                        'token_mode', 'all')
                    if token_mode != TM_FULL_TERM:
                        terms = parse_terms(input_data_field,
                                            case_sensitive=case_sensitive)
                    else:
                        terms = []
                    if token_mode != TM_TOKENS:
                        terms.append(
                            input_data_field if case_sensitive
                            else input_data_field.lower())
                    unique_terms[field_id] = get_unique_terms(
                        terms, self.term_forms[field_id],
                        self.tag_clouds.get(field_id, []))
                else:
                    unique_terms[field_id] = [(input_data_field, 1)]
                del input_data[field_id]
        for field_id in self.categories:
            if field_id in input_data:
                input_data_field = input_data.get(field_id, '')
                if input_data_field in self.categories[field_id]:
                    position = self.categories[field_id].index(
                        input_data_field)
                unique_terms[field_id] = [(input_data_field, 1)]
                del input_data[field_id]
        return unique_terms

    def map_coefficients(self):
        """ Maps each field to the corresponding coefficients subarray

        """
        field_ids = [ \
            field_id for field_id, field in
            sorted(self.fields.items(),
                   key=lambda x: x[1].get("column_number"))]
        shift = 0
        for field_id in field_ids:
            optype = self.fields[field_id]['optype']
            if optype in EXPANSION_ATTRIBUTES.keys():
                length = len(self.fields[field_id]['summary'][ \
                    EXPANSION_ATTRIBUTES[optype]])
            else:
                length = 1
            self.fields[field_id]['coefficients_shift'] = shift
            shift += length
