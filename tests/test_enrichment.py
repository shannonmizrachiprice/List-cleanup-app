from types import SimpleNamespace as NS

import pandas as pd

from listprep import enrichment


def leads():
    return pd.DataFrame({
        "Email Address": ["ada@stripe.com", "bob@gmail.com", "cy@stripe.com", "dee@datadoghq.com"],
        "Company Name": ["Stripe, Inc.", "Stripe", "STRIPE", "Datadog"],
        "Industry": ["", "", "Payments (typed by attendee)", ""],
    })


def test_build_companies_dedupes_and_skips_free_email():
    c = enrichment.build_companies(leads())
    assert list(c["company_name"]) == ["Stripe, Inc.", "Datadog"]
    assert list(c["domain"]) == ["stripe.com", "datadoghq.com"]
    assert list(c["lead_count"]) == [3, 1]


def test_task_spec_schema():
    spec = enrichment.build_task_spec(["Industry", "Number of Employees"])
    schema = spec["output_schema"]["json_schema"]
    assert schema["required"] == ["industry", "employee_count"]
    assert schema["additionalProperties"] is False


def test_recommend_processor():
    assert enrichment.recommend_processor(2) == "lite"
    assert enrichment.recommend_processor(4) == "base"
    assert enrichment.recommend_processor(9) == "core"


class FakeGroups:
    """Mimics client.task_group for the calls run_group makes."""

    def __init__(self, outputs):
        self.outputs = outputs  # company_name -> dict or None (failure)
        self.inputs = []
        self.polls = 0

    def create(self, metadata=None):
        return NS(task_group_id="tgrp_test")

    def add_runs(self, group_id, inputs, default_task_spec):
        self.inputs.extend(inputs)
        return NS(run_ids=[f"run_{i}" for i in range(len(inputs))])

    def retrieve(self, group_id):
        self.polls += 1
        active = self.polls < 2
        return NS(status=NS(is_active=active, task_run_status_counts={"completed": len(self.inputs)}))

    def get_runs(self, group_id, include_output):
        yield NS(type="error")  # ignored
        for i, inp in enumerate(self.inputs):
            content = self.outputs[inp["input"]["company_name"]]
            if content is None:
                yield NS(type="task_run.state", output=None,
                         run=NS(run_id=f"run_{i}", status="failed", error=NS(message="boom")))
                continue
            basis = [NS(field="industry", confidence="high", citations=[NS(url="https://ex.com/a")])]
            yield NS(type="task_run.state", run=NS(run_id=f"run_{i}", status="completed"),
                     output=NS(content=content, basis=basis))


def test_run_group_and_apply():
    companies = enrichment.build_companies(leads())
    fake = FakeGroups({"Stripe, Inc.": {"industry": "Financial Services", "employee_count": "Unknown"},
                       "Datadog": None})
    progress = []
    results = enrichment.run_group(NS(task_group=fake), companies, ["Industry", "Number of Employees"], "base",
                                   on_progress=lambda d, t, c: progress.append((d, t)), poll_seconds=0)

    assert results.attrs["task_group_id"] == "tgrp_test"
    assert progress[-1] == (2, 2)
    stripe = results.set_index("company_key").loc["stripe"]
    assert stripe["Industry"] == "Financial Services"
    assert stripe["Number of Employees"] == ""          # "Unknown" dropped
    assert stripe["Industry sources"] == "https://ex.com/a"
    assert results.set_index("company_key").loc["datadog", "error"] == "boom"

    out, filled = enrichment.apply_enrichment(leads(), results, ["Industry", "Number of Employees"])
    assert list(out["Industry"]) == ["Financial Services", "Financial Services",
                                     "Payments (typed by attendee)", ""]  # attendee value kept
    assert filled == 2

    out, filled = enrichment.apply_enrichment(leads(), results, ["Industry"], overwrite=True)
    assert out.loc[2, "Industry"] == "Financial Services" and filled == 3
